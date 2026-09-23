"""글로벌 매크로 모멘텀 전략 (월간 리밸런싱 + 1시간봉 손절/트레일링/재진입)

v2 변경 요약 (자세한 근거는 같은 폴더의 REVIEW.md)
  [B1] 손절 감시 구간 정렬: 진입 '이전' 가격으로 손절이 나던 문제, 청산일 당일을 감시하지 않던 문제 수정
  [B2] 시간봉이 월 중간부터 시작하는 달: 앞부분이 감시되지 않던 문제 수정 (일봉 + 시간봉 이어붙이기)
  [B3] 일봉 대체값의 미래참조 제거: 날짜 D의 종가는 D+1 00:00 UTC 이후에만 사용
  [B4] SMA 계산의 미래참조 제거 (end_date 이후 데이터 차단)
  [B5] 캐시 조건에 시간봉 범위 포함, 실행부 history_start 불일치로 인한 재다운로드 제거
  [B6] max_positions가 적용되지 않던 문제 수정
  [B7] 손절/재진입 거래를 전체 이력 재탐색 대신 이벤트로 직접 전달 (중복·누락 위험 제거, 속도 개선)
  [R1] 체결 시점 옵션 execute_next_session: 신호일 다음 거래일 종가 체결 (실전과 같은 타이밍)
  [R2] 레버리지 조달비용 반영: 차입분 × (기준금리 + financing_spread)
  [R3] 성과지표: 실제 기간 기준 CAGR, 무위험수익률 차감 샤프
"""
import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import FuncFormatter
from datetime import datetime, timedelta
from pathlib import Path
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)

HOURS_PER_YEAR = 365 * 24


class MomentumStrategy:
    # --- 자산 분류 상수 ---
    CRYPTO_ASSETS = [
        "BTC/USD", "ETH/USD", "TRX/USD", "XRP/USD", "SOL/USD", "BNB/USD", "DOGE/USD",
        "AVAX/USD", "HYPE/USD", "SUI/USD", "ADA/USD", "LINK/USD",
        "XLM/USD", "ZEC/USD", "DOT/USD",
    ]
    COMMODITY_ASSETS = ["원유", "구리", "농산물", "금", "은"]
    BOND_ASSETS = ["미국 20년 국채 ETF"]
    MAX_LEVERAGE_CAP = {"BNB-USD": 2.3, "SOL-USD": 2.8, "XRP-USD": 1.5}

    def __init__(self, tickers_dict, initial_capital=100000000,
                 momentum_threshold_min=1.2, momentum_threshold_max=3.0,
                 max_positions=8, max_crypto_positions=4, max_non_crypto_positions=4,
                 # 3단계 레버리지
                 base_leverage=2.0,
                 leverage_threshold_1=1.2, leverage_multiplier_1=1.5,
                 leverage_threshold_2=1.5, leverage_multiplier_2=2.0,
                 leverage_threshold_3=2.0, leverage_multiplier_3=2.9,
                 transaction_cost=0.005, sma_filter_months=6,
                 macro_filter_ticker='^GSPC', macro_filter_sma_months=10,
                 bond_filter_ticker='SHY', bond_filter_sma_months=6,
                 stop_loss_pct=-0.08, reentry_threshold_pct=0.01, max_reentry_count=2,
                 reentry_cooldown_hours=72,
                 enable_trailing_stop=True, default_trailing_pct=0.05,
                 crypto_trailing_pct=0.08, bond_trailing_pct=0.03, commodity_trailing_pct=0.06,
                 # [R1] True: 신호일(월말) 다음 거래일 종가에 체결 / False: 신호일 종가에 즉시 체결(v1 동작)
                 execute_next_session=True,
                 # [R2] 레버리지 차입분 연 조달금리 = 기준금리 + spread (None이면 조달비용 미반영, v1 동작)
                 financing_spread=0.02):

        self.tickers_dict = tickers_dict
        self.initial_capital = initial_capital
        self.momentum_threshold_min = momentum_threshold_min
        self.momentum_threshold_max = momentum_threshold_max
        self.max_positions = max_positions
        self.max_crypto_positions = max_crypto_positions
        self.max_non_crypto_positions = max_non_crypto_positions
        self.stop_loss_pct = stop_loss_pct
        self.reentry_threshold_pct = reentry_threshold_pct
        self.max_reentry_count = max_reentry_count
        self.reentry_cooldown_hours = reentry_cooldown_hours
        self.transaction_cost = transaction_cost
        self.sma_filter_months = sma_filter_months
        self.macro_filter_ticker = macro_filter_ticker
        self.macro_filter_sma_months = macro_filter_sma_months
        self.bond_filter_ticker = bond_filter_ticker
        self.bond_filter_sma_months = bond_filter_sma_months
        self.execute_next_session = execute_next_session
        self.financing_spread = financing_spread

        # 레버리지 설정 — 높은 모멘텀일수록 낮은 레버리지(역전)는 사용자 의도, 유지.
        self.base_leverage = base_leverage
        self.leverage_tiers = [
            (leverage_threshold_3, leverage_multiplier_3),
            (leverage_threshold_2, leverage_multiplier_2),
            (leverage_threshold_1, leverage_multiplier_1),
        ]

        self.enable_trailing_stop = enable_trailing_stop
        self.trailing_pct_map = {
            'crypto': crypto_trailing_pct,
            'bond': bond_trailing_pct,
            'commodity': commodity_trailing_pct,
            'default': default_trailing_pct,
        }

        # 상태 추적
        self.high_water_marks = {}          # [B7] ticker -> 최고가(float). v1은 모든 시각을 dict로 쌓아 O(n²)
        self.reentry_count_tracker = {}
        self.stop_loss_timestamps = {}
        self.total_transaction_costs = 0
        self.total_financing_costs = 0
        self.total_transaction_volume = 0
        self.is_risk_on = True

        # 이력 저장
        self.price_data = None              # 일봉 (ffill)
        self.raw_price_data = None          # 일봉 (ffill 전: 실제 거래일 판별용)
        self.hourly_price_data = None
        self.monthly_returns = []
        self.selected_assets_history = []
        self.stop_loss_history = []
        self.reentry_history = []
        self.trailing_stop_history = []
        self.monthly_leverage_history = []
        self.transaction_history = []
        self.current_holdings_detail = []
        self.bok_rates_data = self._build_bok_rates()

        self._data_start = None
        self._data_end = None
        self._hourly_start = None
        self._bt_start = None

        self._print_init_info()

    # ──────────────────────── 유틸리티 ────────────────────────

    def _asset_type(self, name):
        if name in self.CRYPTO_ASSETS: return 'crypto'
        if name in self.BOND_ASSETS: return 'bond'
        if name in self.COMMODITY_ASSETS: return 'commodity'
        return 'default'

    def get_asset_trailing_pct(self, name):
        return self.trailing_pct_map[self._asset_type(name)]

    @staticmethod
    def _to_utc(dt):
        dt = pd.Timestamp(dt)
        return dt.tz_localize('UTC') if dt.tzinfo is None else dt.tz_convert('UTC')

    @staticmethod
    def _next_day_utc(date_like):
        """일봉 날짜 D의 종가가 '확정되어 사용 가능한' 시각 = D+1 00:00 UTC (보수적 근사).
        크립토(UTC 일봉)는 정확히 일치, 미국장(~20:00 UTC 마감)·한국장(06:30 UTC 마감)은 그보다 이르게 확정."""
        d = pd.Timestamp(date_like).normalize()
        if d.tzinfo is not None:
            d = d.tz_convert('UTC').tz_localize(None)
        return (d + pd.Timedelta(days=1)).tz_localize('UTC')

    def get_leverage_for_momentum(self, score, ticker=None):
        if not self.is_risk_on:
            return 1.0
        lev = self.base_leverage
        for threshold, multiplier in self.leverage_tiers:
            if score >= threshold:
                lev = multiplier
                break
        if ticker and ticker in self.MAX_LEVERAGE_CAP:
            lev = min(lev, self.MAX_LEVERAGE_CAP[ticker])
        return lev

    def get_bok_rate(self, date_str):
        key = str(date_str)[:7]
        if key in self.bok_rates_data:
            return self.bok_rates_data[key]
        available = sorted(k for k in self.bok_rates_data if k <= key)
        return self.bok_rates_data[available[-1]] if available else 3.50

    def _ticker_to_name(self, ticker):
        return next((k for k, v in self.tickers_dict.items() if v == ticker), ticker)

    # ──────────────────────── 데이터 ────────────────────────

    def get_trading_day_price(self, ticker, date_str):
        """date_str 시점까지의 마지막 일봉 종가 (ffill)"""
        try:
            price = self.price_data[ticker].asof(pd.Timestamp(date_str))
            return float(price) if pd.notna(price) else None
        except (KeyError, IndexError, TypeError):
            return None

    def _daily_close_known_at(self, ticker, dt):
        """[B3] 시각 dt에 '이미 확정된' 마지막 일봉 종가. v1은 D일 00:00에 D일 종가를 써서 미래참조."""
        if self.price_data is None or ticker not in self.price_data.columns:
            return None
        naive = self._to_utc(dt).tz_localize(None) - pd.Timedelta(days=1)
        price = self.price_data[ticker].asof(naive)
        return float(price) if pd.notna(price) else None

    def get_hourly_price(self, ticker, dt):
        """시간봉 가격(해당 시각 이전 마지막 값). 없으면 확정된 일봉 종가로 대체."""
        dt = self._to_utc(dt)
        if self.hourly_price_data is not None and ticker in self.hourly_price_data.columns:
            price = self.hourly_price_data[ticker].asof(dt)
            if pd.notna(price):
                return float(price)
        return self._daily_close_known_at(ticker, dt)

    def get_latest_price(self, ticker):
        """가장 최근 가격 (1시간봉 우선, 없으면 일봉) — '현재가' 표시용"""
        for df in (self.hourly_price_data, self.price_data):
            if df is not None and ticker in df.columns:
                s = df[ticker].dropna()
                if not s.empty:
                    return float(s.iloc[-1])
        return None

    def _monthly_last(self, ticker, end_date):
        """end_date까지의 월별 마지막 종가 (Period 인덱스). [B4] end_date 이후 데이터는 절대 사용하지 않음"""
        s = self.price_data[ticker].loc[:pd.Timestamp(end_date)].dropna()
        if s.empty:
            return s
        return s.groupby(s.index.to_period('M')).last()

    def _calculate_monthly_sma(self, ticker, end_date_str, months):
        try:
            ml = self._monthly_last(ticker, end_date_str)
        except KeyError:
            return None
        end_p = pd.Timestamp(end_date_str).to_period('M')
        wanted = [end_p - i for i in range(months)]
        vals = [ml[p] for p in wanted if p in ml.index]
        return sum(vals) / len(vals) if len(vals) >= months * 0.8 else None

    def calculate_momentum_score(self, ticker, end_date_str):
        try:
            ml = self._monthly_last(ticker, end_date_str)
        except KeyError:
            return None
        if ml.empty:
            return None
        current = self.get_trading_day_price(ticker, end_date_str)
        if current is None:
            return None
        end_p = pd.Timestamp(end_date_str).to_period('M')
        ratios = [current / ml[end_p - m] for m in range(6, 12)
                  if (end_p - m) in ml.index and ml[end_p - m] > 0]
        if len(ratios) < 4:
            return None
        return {'score': sum(ratios) / len(ratios), 'current_price': current}

    def get_month_end_dates(self, start, end):
        idx = self.price_data.loc[start:end].index
        last = pd.Series(idx, index=idx).groupby(idx.to_period('M')).max()
        return [d.strftime('%Y-%m-%d') for d in sorted(last)]

    def _prepare_data(self, start_str, end_str):
        start = datetime.strptime(start_str, '%Y-%m-%d')
        offset = max(14, self.sma_filter_months + 2, self.macro_filter_sma_months + 2, self.bond_filter_sma_months + 2)
        data_start = start - pd.DateOffset(months=offset)
        end = datetime.strptime(end_str, '%Y-%m-%d')
        h_start = max(start - timedelta(days=7), datetime.now() - timedelta(days=729))

        # [B5] 캐시: 일봉 범위뿐 아니라 시간봉 시작 시점도 포함해야 재사용
        if (self.price_data is not None and self._data_start is not None
                and self._data_start <= data_start and self._data_end >= end
                and self._hourly_start is not None and self._hourly_start <= h_start):
            print("✓ 기존 다운로드 데이터 재사용 (재다운로드 생략)")
            return

        print("\n전체 자산 데이터 다운로드 중...")
        all_tickers = list(self.tickers_dict.values())
        for t in [self.macro_filter_ticker, self.bond_filter_ticker]:
            if t and t not in all_tickers:
                all_tickers.append(t)

        all_data, all_hourly = {}, {}
        print(f"  일봉 다운로드 중... ({len(all_tickers)}개 티커, 병렬)")
        daily = yf.download(all_tickers, start=data_start, end=end + timedelta(days=1),
                            progress=False, threads=True, auto_adjust=True)
        self._extract_prices(daily, all_tickers, all_data)

        if h_start > start - timedelta(days=7):
            print(f"  ⚠ 1시간봉은 yfinance 한도(약 729일)로 {h_start:%Y-%m-%d} 이후만 제공됩니다.")
            print(f"    → 그 이전 구간의 손절/트레일링은 확정된 일봉 종가 기준으로 체크합니다.")

        print(f"  1시간봉 다운로드 중... ({len(all_tickers)}개 티커, 병렬)")
        hourly = yf.download(all_tickers, start=h_start, end=end + timedelta(days=1),
                             interval='1h', progress=False, threads=True, auto_adjust=True)
        self._extract_prices(hourly, all_tickers, all_hourly)

        missing = [t for t in all_tickers if t not in all_data]
        if missing:
            print(f"  ⚠ 일봉 누락 티커: {missing}")

        raw = pd.DataFrame(all_data)
        if not raw.empty and raw.index.tz is not None:
            raw.index = raw.index.tz_localize(None)
        self.raw_price_data = raw.sort_index()
        self.price_data = self.raw_price_data.ffill()

        hp = pd.DataFrame(all_hourly)
        if not hp.empty:
            hp.index = hp.index.tz_localize('UTC') if hp.index.tz is None else hp.index.tz_convert('UTC')
            hp = hp.sort_index().ffill()
        self.hourly_price_data = hp

        self._data_start, self._data_end, self._hourly_start = data_start, end, h_start
        print("✓ 데이터 다운로드 완료 (일봉 + 1시간봉, UTC)")

    @staticmethod
    def _extract_prices(df, tickers, out_dict):
        if df is None or df.empty:
            return
        if isinstance(df.columns, pd.MultiIndex):
            col = 'Adj Close' if 'Adj Close' in df.columns.get_level_values(0) else 'Close'
            for t in tickers:
                if t in df[col].columns and df[col][t].notna().any():
                    out_dict[t] = df[col][t]
        else:
            s = df.get('Adj Close', df.get('Close'))
            if s is not None:
                out_dict[tickers[0]] = s

    # ──────────────────────── 체결 시점 ────────────────────────

    def _exec_point(self, ticker, signal_date):
        """[R1] (체결가, 체결가 확정 시각 UTC).
        execute_next_session=False → 신호일 종가(ffill)에 체결 (v1 동작, 같은 종가로 신호+체결 → 낙관적)
        execute_next_session=True  → 해당 자산의 신호일 '다음 실제 거래일' 종가에 체결
        다음 거래일 데이터가 아직 없으면(최신 구간) 신호일 종가로 평가(mark-to-market)."""
        if self.execute_next_session and self.raw_price_data is not None and ticker in self.raw_price_data:
            s = self.raw_price_data[ticker].dropna()
            after = s.loc[s.index > pd.Timestamp(signal_date)]
            if not after.empty:
                return float(after.iloc[0]), self._next_day_utc(after.index[0])
        p = self.get_trading_day_price(ticker, signal_date)
        return (p, self._next_day_utc(signal_date)) if p else (None, None)

    # ──────────────────────── Trailing Stop ────────────────────────

    def _check_trailing_stop(self, asset, buy_price, current_price):
        ticker = asset['ticker']
        hwm = max(self.high_water_marks.get(ticker, current_price), current_price)
        self.high_water_marks[ticker] = hwm
        if not self.enable_trailing_stop:
            return False, None
        ret = (current_price - buy_price) / buy_price
        if ret <= 0:
            return False, None
        trailing_pct = self.get_asset_trailing_pct(asset['name'])
        if current_price <= hwm * (1 - trailing_pct):
            return True, {
                'type': 'trailing_stop', 'high_water_mark': hwm,
                'trailing_pct': trailing_pct, 'stop_price': current_price,
                'buy_price': buy_price, 'return_pct': ret * 100
            }
        return False, None

    # ──────────────────────── 손절매 / 재진입 ────────────────────────

    def check_stop_loss_and_reentry_hourly(self, holding, stopped, current_dt, current_month, price_row=None):
        """holding: 보유 자산 dict 리스트, stopped: [{'info': 손절기록, 'asset': 원 자산 dict}]
        반환: (holding, stopped, 이번 시각 손절 이벤트 리스트, 이번 시각 재진입 이벤트 리스트)
        [B7] 이벤트를 직접 반환 → 호출부가 이력 전체를 타임스탬프로 재탐색하지 않음.
        각 자산은 자신의 [entry_ts, exit_ts) 구간 안에서만 감시."""
        current_dt = self._to_utc(current_dt)

        def _px(ticker):
            if price_row is not None:
                row, cidx = price_row
                j = cidx.get(ticker)
                if j is not None and row[j] == row[j]:
                    return float(row[j])
            return self.get_hourly_price(ticker, current_dt)

        def _active(a):
            return a['entry_ts'] <= current_dt < a['exit_ts']

        stop_events, reentry_events = [], []
        remaining, new_stopped = [], list(stopped)

        # 1) 보유 자산 체크
        for asset in holding:
            if not _active(asset):
                remaining.append(asset)
                continue
            ticker = asset['ticker']
            bp = asset.get('reentry_price') or asset['entry_price']
            cp = _px(ticker)
            if not (bp and cp and bp > 0):
                remaining.append(asset)
                continue

            ret = (cp - bp) / bp
            ts_hit, ts_info = self._check_trailing_stop(asset, bp, cp)
            if ts_hit or ret <= self.stop_loss_pct:
                stop_type = 'trailing' if ts_hit else 'fixed'
                si = {
                    'date': current_dt, 'asset': asset['name'], 'ticker': ticker,
                    'buy_price': bp, 'sell_price': cp, 'stop_price': cp,
                    'loss_pct': ret * 100, 'weight': asset['target_weight'],
                    'is_reentry_stop': 'reentry_price' in asset,
                    'leverage': asset['leverage'], 'month': current_month, 'stop_type': stop_type,
                }
                self.stop_loss_history.append(si)
                stop_events.append(si)
                new_stopped.append({'info': si, 'asset': asset})
                self.stop_loss_timestamps[ticker] = current_dt
                dt_str = current_dt.strftime('%Y-%m-%d %H:%M UTC')
                if ts_hit:
                    self.trailing_stop_history.append({**ts_info, 'date': current_dt, 'asset': asset['name']})
                    print(f"    📉 Trailing Stop [{dt_str}]: {asset['name']} "
                          f"(HWM {ts_info['high_water_mark']:.2f}→{cp:.2f}, {ts_info['return_pct']:.2f}%)")
                else:
                    tag = "🛑🔄 재진입후 손절" if 'reentry_price' in asset else "🛑 고정 손절"
                    print(f"    {tag} [{dt_str}]: {asset['name']} ({ret*100:.2f}%)")
            else:
                remaining.append(asset)

        # 2) 재진입 체크
        remaining_stopped = []
        counts = self.reentry_count_tracker.setdefault(current_month, {})
        for st in new_stopped:
            sa, orig = st['info'], st['asset']
            ticker, sp = sa['ticker'], sa['stop_price']
            if not _active(orig):
                remaining_stopped.append(st)
                continue
            cp = _px(ticker)
            if not (cp and sp and sp > 0):
                remaining_stopped.append(st)
                continue
            count = counts.get(ticker, 0)
            can_time, _ = self._can_reenter_time(ticker, current_dt)
            if cp >= sp * (1 + self.reentry_threshold_pct) and count < self.max_reentry_count and can_time:
                counts[ticker] = count + 1
                ra = {**orig, 'reentry_price': cp, 'stop_price': sp, 'reentry_count': count + 1}
                remaining.append(ra)
                self.high_water_marks[ticker] = cp
                ev = {
                    'date': current_dt, 'asset': sa['asset'], 'ticker': ticker,
                    'stop_price': sp, 'reentry_price': cp,
                    'reentry_pct': ((cp - sp) / sp) * 100,
                    'leverage': orig['leverage'], 'weight': orig['target_weight'],
                    'reentry_count': count + 1, 'month': current_month,
                }
                self.reentry_history.append(ev)
                reentry_events.append(ev)
                print(f"    ✅ 재진입 [{current_dt:%Y-%m-%d %H:%M} UTC] ({count+1}/{self.max_reentry_count}): {sa['asset']}")
            else:
                remaining_stopped.append(st)

        return remaining, remaining_stopped, stop_events, reentry_events

    def _can_reenter_time(self, ticker, current_dt):
        if ticker not in self.stop_loss_timestamps:
            return True, 0
        elapsed = (current_dt - self._to_utc(self.stop_loss_timestamps[ticker])).total_seconds() / 3600
        return elapsed >= self.reentry_cooldown_hours, max(0, self.reentry_cooldown_hours - elapsed)

    # ──────────────────────── 감시 타임라인 ────────────────────────

    def _build_timeline(self, start_ts, end_ts):
        """[B1][B2] (start_ts, end_ts) 사이의 감시 시각 목록 [(ts, price_row or None)].
        시간봉이 있는 구간은 시간봉, 시간봉 이전 구간은 '확정 일봉 종가'(D+1 00:00 UTC) 시각으로 채움.
        v1은 시간봉이 월 중간부터 시작하면 그 앞부분을 통째로 감시하지 않았음."""
        events = []
        h_first = end_ts
        if self.hourly_price_data is not None and not self.hourly_price_data.empty:
            hp = self.hourly_price_data
            hp = hp.loc[(hp.index >= start_ts) & (hp.index < end_ts)]
            if len(hp):
                h_first = hp.index[0]
                arr = hp.to_numpy(dtype=float)
                cidx = {c: j for j, c in enumerate(hp.columns)}
                hourly_events = [(ts, (arr[i], cidx)) for i, ts in enumerate(hp.index)]
            else:
                hourly_events = []
        else:
            hourly_events = []

        for d in self.price_data.index:
            ts = self._next_day_utc(d)
            if start_ts < ts < h_first and ts <= end_ts:
                events.append((ts, None))
        return events + hourly_events

    # ──────────────────────── 월별 수익률 계산 ────────────────────────

    def calculate_monthly_return(self, selected, buy_date, sell_date, pv, record_detail=False):
        if record_detail:
            self.current_holdings_detail = []

        rate = self.get_bok_rate(sell_date)
        period_start = self._next_day_utc(buy_date)
        period_end = self._next_day_utc(sell_date)
        period_h = max((period_end - period_start).total_seconds() / 3600, 0)

        if not selected:
            r = (rate / 100) * period_h / HOURS_PER_YEAR
            print(f"현금 투자 - 수익률: {r*100:.4f}% (기준금리: {rate}%)")
            return r

        current_month = pd.Timestamp(sell_date).strftime('%Y-%m')
        self.reentry_count_tracker.setdefault(current_month, {})

        # 자산별 진입/청산 시점·가격 확정
        holding = []
        for a in selected:
            ep, ets = self._exec_point(a['ticker'], buy_date)
            xp, xts = self._exec_point(a['ticker'], sell_date)
            if not ep or not xp:
                print(f"  ⚠ {a['name']}: 진입/청산 가격 없음 → 해당 비중 현금 처리")
                continue
            a = {**a,
                 'leverage': self.get_leverage_for_momentum(a.get('momentum_score', 0), ticker=a['ticker']),
                 'entry_price': ep, 'entry_ts': ets, 'exit_price': xp, 'exit_ts': xts}
            holding.append(a)
            self.high_water_marks[a['ticker']] = ep

        txns = {a['ticker']: [] for a in holding}
        monthly_cost = 0.0

        def _record(ticker, kind, ts, price, lev, weight, reason):
            nonlocal monthly_cost
            amt = pv * weight * lev
            monthly_cost += amt * self.transaction_cost
            self.total_transaction_volume += amt
            eff = price * (1 + self.transaction_cost) if kind == 'buy' else price * (1 - self.transaction_cost)
            txns[ticker].append({'type': kind, 'date': ts, 'price': price, 'effective_price': eff,
                                 'leverage': lev, 'reason': reason})

        for a in holding:
            _record(a['ticker'], 'buy', a['entry_ts'], a['entry_price'], a['leverage'], a['target_weight'], 'rebalance')

        if holding:
            start_ts = min(a['entry_ts'] for a in holding)
            end_ts = max(a['exit_ts'] for a in holding)
            timeline = self._build_timeline(start_ts, end_ts)
            if not any(pr is not None for _, pr in timeline):
                print(f"  ⚠ {current_month}: 시간봉 없음 → 확정 일봉 종가 기준으로 손절/트레일링 체크")

            stopped = []
            for ts, price_row in timeline:
                if not holding and not stopped:
                    break
                holding, stopped, stops, reentries = self.check_stop_loss_and_reentry_hourly(
                    holding, stopped, ts, current_month, price_row=price_row)
                for sl in stops:
                    _record(sl['ticker'], 'sell', ts, sl['sell_price'], sl['leverage'], sl['weight'], sl['stop_type'])
                for r in reentries:
                    _record(r['ticker'], 'buy', ts, r['reentry_price'], r['leverage'], r['weight'], 'reentry')

            # 월말(리밸런싱) 청산
            for a in holding:
                _record(a['ticker'], 'sell', a['exit_ts'], a['exit_price'], a['leverage'], a['target_weight'], 'month_end')

        # 복리 계산
        total_final = 0.0
        fin_annual = None if self.financing_spread is None else rate / 100 + self.financing_spread
        weights = {a['ticker']: a['target_weight'] for a in selected}
        for ticker, tx_list in txns.items():
            w = weights[ticker]
            cum_val = pv * w
            hold_h = 0.0
            open_buy = None
            for tx in tx_list:
                if tx['type'] == 'buy':
                    open_buy = tx
                elif open_buy is not None:
                    hours = (tx['date'] - open_buy['date']).total_seconds() / 3600
                    ret = (tx['effective_price'] - open_buy['effective_price']) / open_buy['effective_price']
                    lev = open_buy['leverage']
                    lev_ret = ret * lev
                    if fin_annual is not None and lev > 1 and hours > 0:
                        fin = fin_annual * (lev - 1) * hours / HOURS_PER_YEAR
                        lev_ret -= fin
                        self.total_financing_costs += cum_val * fin
                    cum_val *= (1 + lev_ret)
                    hold_h += max(hours, 0)
                    open_buy = None

            cash_h = period_h - hold_h
            if cash_h > 0:
                cum_val *= (1 + (rate / 100) * cash_h / HOURS_PER_YEAR)

            if record_detail:
                buys = [t for t in tx_list if t['type'] == 'buy']
                n_reentry = sum(1 for t in buys if t['reason'] == 'reentry')
                last_tx = tx_list[-1]
                if last_tx['reason'] == 'month_end':
                    status = "보유중" + (f" (재진입 {n_reentry}회)" if n_reentry else "")
                else:
                    status = f"손절청산→현금 ({last_tx['reason']})"
                self.current_holdings_detail.append({
                    'name': self._ticker_to_name(ticker), 'ticker': ticker,
                    'entry_price': buys[0]['price'] if buys else None,
                    'current_price': self.get_latest_price(ticker),
                    'weight': w, 'leverage': buys[0]['leverage'] if buys else None,
                    'position_return': cum_val / (pv * w) - 1,
                    'status': status, 'reentry_count': n_reentry,
                })
            total_final += cum_val

        # 미배정 비중(가상화폐만 선택 시 50% 등) + 가격 없어 제외된 자산 비중 → 현금
        invested_w = sum(weights[t] for t in txns)
        cash_w = 1.0 - invested_w
        if cash_w > 1e-9:
            total_final += pv * cash_w * (1 + (rate / 100) * period_h / HOURS_PER_YEAR)

        total_ret = (total_final - pv) / pv
        self.transaction_history.append({
            'date': sell_date, 'monthly_cost': monthly_cost,
            'portfolio_value': pv, 'cost_pct_of_portfolio': monthly_cost / pv * 100 if pv else 0
        })
        self.total_transaction_costs += monthly_cost
        print(f"  💰 거래비용: {monthly_cost:,.0f}원 ({monthly_cost/pv*100:.4f}%)")
        return total_ret

    # ──────────────────────── 모멘텀 분석 ────────────────────────

    def analyze_monthly_momentum(self, date_str, show_detail=False):
        print(f"\n=== {date_str} 모멘텀 분석 ===")
        macro_on = self._check_macro_filter(date_str)
        bond_on = self._check_bond_filter(date_str)
        self.is_risk_on = macro_on and bond_on

        print(f"  → 최종 위험: {'ON' if self.is_risk_on else 'OFF'}")
        if not self.is_risk_on:
            print(f"  위험 OFF → 현금 보유 (기준금리: {self.get_bok_rate(date_str)}%)")
            if show_detail:
                print(f"\n  [참고] 개별 자산 모멘텀 현황 (위험 OFF — 투자 미실행):")
                for name, ticker in self.tickers_dict.items():
                    r = self.calculate_momentum_score(ticker, date_str)
                    if r is None:
                        print(f"    - {name}: 데이터 부족")
                        continue
                    passed, info = self._check_sma_filter(ticker, date_str, r['current_price'])
                    self.is_risk_on = True
                    lev = self.get_leverage_for_momentum(r['score'], ticker)
                    self.is_risk_on = False
                    print(f"    {'✓' if passed else '✗'} {name}: 모멘텀 {r['score']:.3f}, "
                          f"가격 {r['current_price']:.2f} {info} (참고 레버리지: {lev}x)")
            return []

        results = []
        for name, ticker in self.tickers_dict.items():
            r = self.calculate_momentum_score(ticker, date_str)
            if r is None:
                print(f"  - {name}: 데이터 부족")
                continue
            passed, info = self._check_sma_filter(ticker, date_str, r['current_price'])
            print(f"  {'✓' if passed else '✗'} {name}: 모멘텀 {r['score']:.3f} {info}")
            if passed:
                results.append({'name': name, 'ticker': ticker, 'momentum_score': r['score'], 'price': r['current_price']})

        results.sort(key=lambda x: x['momentum_score'], reverse=True)
        qualified = [a for a in results if self._passes_momentum_threshold(a)]
        final = self._apply_crypto_weight_limit(qualified)

        print(f"\n선택 자산 ({len(final)}개):")
        for i, a in enumerate(final, 1):
            lev = self.get_leverage_for_momentum(a['momentum_score'], a['ticker'])
            tp = self.get_asset_trailing_pct(a['name'])
            print(f"  {i}. {a['name']}: {a['momentum_score']:.3f} (비중 {a['target_weight']*100:.1f}%, {lev}x, TS {tp*100:.1f}%)")
        if not final:
            print(f"  → 현금 투자 (기준금리: {self.get_bok_rate(date_str)}%)")
        return final

    def _check_macro_filter(self, date_str):
        if not self.macro_filter_ticker:
            return True
        cp = self.get_trading_day_price(self.macro_filter_ticker, date_str)
        sma = self._calculate_monthly_sma(self.macro_filter_ticker, date_str, self.macro_filter_sma_months)
        if cp is None or sma is None:
            print("  [S&P500] 데이터 부족 → OFF")
            return False
        threshold = sma * 1.01
        on = cp > threshold
        print(f"  [S&P500] {'ON' if on else 'OFF'} (현재 {cp:.2f} vs SMA*1.01 {threshold:.2f})")
        return on

    def _check_bond_filter(self, date_str):
        if not self.bond_filter_ticker:
            return True
        cp = self.get_trading_day_price(self.bond_filter_ticker, date_str)
        sma = self._calculate_monthly_sma(self.bond_filter_ticker, date_str, self.bond_filter_sma_months)
        if cp is None or sma is None:
            print("  [금리] 데이터 부족 → OFF")
            return False
        on = cp >= sma
        print(f"  [금리] {'ON' if on else 'OFF'} (국채 {cp:.2f} vs SMA {sma:.2f})")
        return on

    def _check_sma_filter(self, ticker, date_str, current_price):
        if self.sma_filter_months <= 0:
            return True, ""
        sma = self._calculate_monthly_sma(ticker, date_str, self.sma_filter_months)
        if sma is None:
            return False, "(SMA 데이터 부족)"
        passed = current_price > sma
        return passed, f"({'통과' if passed else '미통과'}: {current_price:.2f} vs SMA {sma:.2f})"

    def _passes_momentum_threshold(self, asset):
        s = asset['momentum_score']
        name = asset['name']
        if name in self.CRYPTO_ASSETS:
            return s >= 1.2
        if name == "미국 20년 국채 ETF":
            return 1.1 <= s < self.momentum_threshold_max
        if name in ["원유", "구리", "농산물"]:
            return 1.2 <= s < 2.5
        if name == "달러 인덱스":
            return 1.05 <= s < 1.5
        return self.momentum_threshold_min <= s < self.momentum_threshold_max

    def _apply_crypto_weight_limit(self, qualified):
        """모멘텀 내림차순으로 자산군별 한도 + [B6] 전체 한도(max_positions)를 함께 적용"""
        cryptos, others = [], []
        for a in qualified:
            if len(cryptos) + len(others) >= self.max_positions:
                break
            if a['name'] in self.CRYPTO_ASSETS:
                if len(cryptos) < self.max_crypto_positions:
                    cryptos.append(a)
            elif len(others) < self.max_non_crypto_positions:
                others.append(a)

        if not cryptos and not others:
            return []
        if not others:
            w = 0.5 / len(cryptos)
            for c in cryptos: c['target_weight'] = w
            return cryptos
        if not cryptos:
            w = 1.0 / len(others)
            for o in others: o['target_weight'] = w
            return others

        total = cryptos + others
        eq_w = 1.0 / len(total)
        if eq_w * len(cryptos) > 0.5:
            cw, ow = 0.5 / len(cryptos), 0.5 / len(others)
            for c in cryptos: c['target_weight'] = cw
            for o in others: o['target_weight'] = ow
        else:
            for a in total: a['target_weight'] = eq_w
        return total

    # ──────────────────────── 현재 포트폴리오 / 주문표 ────────────────────────

    def _last_completed_month_end(self):
        idx = self.price_data.index
        cur = pd.Timestamp(datetime.now()).to_period('M')
        prev = idx[idx.to_period('M') < cur]
        return prev.max().strftime('%Y-%m-%d') if len(prev) else None

    def analyze_current_portfolio(self, history_start="2024-07-31"):
        """규칙상 지금 보유해야 할 포트폴리오 = '직전 월말' 신호.
        오늘 날짜 신호는 다음 리밸런싱 참고용 미리보기로 따로 출력합니다.
        (v1은 오늘(월 중간) 신호를 '현재 포트폴리오'로 보여 줘 규칙과 달랐음)"""
        print(f"\n{'='*80}\n현재 포트폴리오 분석\n{'='*80}")
        today = datetime.now().strftime('%Y-%m-%d')
        self._prepare_data(history_start, today)
        if self.price_data is None or self.price_data.empty:
            print("데이터 로드 실패")
            return None

        signal_day = self._last_completed_month_end()
        print(f"\n[1] 지금 보유해야 할 포트폴리오 — 직전 월말 신호 ({signal_day})")
        selected = self.analyze_monthly_momentum(signal_day)
        self._print_portfolio_summary(selected, signal_day)

        last_day = self.price_data.index.max().strftime('%Y-%m-%d')
        print(f"\n[2] 오늘 기준 미리보기 ({last_day}) — 체결 대상 아님, 월말 신호가 바뀔 가능성 참고용")
        preview = self.analyze_monthly_momentum(last_day, show_detail=True)
        self._print_portfolio_summary(preview, last_day)
        return selected

    def _print_portfolio_summary(self, selected, date_str):
        print(f"\n{'='*60}\n📊 포트폴리오 요약 ({date_str})\n{'='*60}")
        if not selected:
            print(f"❌ 투자 대상 없음 → 현금 (기준금리: {self.get_bok_rate(date_str)}%)")
            return
        for a in selected:
            lev = self.get_leverage_for_momentum(a['momentum_score'], a['ticker'])
            tp = self.get_asset_trailing_pct(a['name'])
            print(f"  {a['name']}: 비중 {a['target_weight']*100:.1f}%, 점수 {a['momentum_score']:.3f}, {lev}x, TS {tp*100:.1f}%")

    def build_order_sheet(self, selected, portfolio_value):
        """실전 주문 준비용 목표 포지션표 (주문 전송은 하지 않음).
        명목금액 = 자본 × 비중 × 레버리지. 가격은 원통화 기준이며 환산은 별도로 해야 함."""
        rows = []
        for a in selected:
            lev = self.get_leverage_for_momentum(a['momentum_score'], a['ticker'])
            ref = self.get_latest_price(a['ticker'])
            rows.append({
                'name': a['name'], 'ticker': a['ticker'], 'weight': a['target_weight'], 'leverage': lev,
                'notional_krw': portfolio_value * a['target_weight'] * lev,
                'ref_price': ref,
                'fixed_stop_price': ref * (1 + self.stop_loss_pct) if ref else None,
                'trailing_pct': self.get_asset_trailing_pct(a['name']),
            })
        return pd.DataFrame(rows)

    def _print_current_holdings(self, entry_date, current_date):
        print(f"\n{'='*72}")
        print(f"💼 현재 보유 포트폴리오  (신호일 {entry_date} → 현재 {current_date})")
        print(f"{'='*72}")
        if not self.current_holdings_detail:
            print("  보유 자산 없음 → 현금 100% (기준금리 적용)")
            return
        for h in sorted(self.current_holdings_detail, key=lambda x: x['weight'], reverse=True):
            ep = f"{h['entry_price']:,.4f}" if h['entry_price'] else "N/A"
            cp = f"{h['current_price']:,.4f}" if h['current_price'] else "N/A"
            raw = ""
            if h['entry_price'] and h['current_price']:
                raw = f" (원자산 {(h['current_price'] / h['entry_price'] - 1) * 100:+.2f}%)"
            lev = f"{h['leverage']:.1f}x" if h['leverage'] else "-"
            print(f"  • {h['name']:<12} 진입 {ep:>13} → 현재 {cp:>13} | "
                  f"비중 {h['weight']*100:4.1f}% | {lev} | "
                  f"포지션수익률 {h['position_return']*100:+6.2f}%{raw} | {h['status']}")
        weighted = sum(h['position_return'] * h['weight'] for h in self.current_holdings_detail)
        invested_w = sum(h['weight'] for h in self.current_holdings_detail)
        print(f"  {'-'*68}")
        print(f"  투자 비중 합계: {invested_w*100:.1f}% | 현금 비중: {(1-invested_w)*100:.1f}%")
        print(f"  포지션 가중합 수익률(미배정 현금 이자 제외): {weighted*100:+.2f}%")
        print(f"  ※ 포지션수익률 = 레버리지·거래비용·조달비용·손절/재진입 반영")
        print(f"  ※ 원자산% = 진입가 대비 현재가 단순 변동(레버리지·비용 미반영)")

    # ──────────────────────── 백테스트 ────────────────────────

    def run_backtest(self, start_date, end_date):
        print(f"\n{'='*60}\n백테스트 ({start_date} ~ {end_date})\n{'='*60}")
        print(f"  체결: {'신호 다음 거래일 종가' if self.execute_next_session else '신호일 종가(낙관적)'} | "
              f"조달비용: {'미반영' if self.financing_spread is None else f'기준금리+{self.financing_spread*100:.1f}%p'}")
        self._prepare_data(start_date, end_date)
        if self.price_data is None or self.price_data.empty:
            return

        dates = self.get_month_end_dates(start_date, end_date)
        if len(dates) < 2:
            print("최소 2개월 필요")
            return

        pv = self.initial_capital
        self._bt_start = dates[0]
        self.monthly_returns, self.total_transaction_costs, self.total_financing_costs = [], 0, 0
        self.total_transaction_volume = 0
        self.reentry_count_tracker, self.high_water_marks, self.stop_loss_timestamps = {}, {}, {}
        self.stop_loss_history, self.reentry_history, self.trailing_stop_history = [], [], []
        self.monthly_leverage_history, self.transaction_history = [], []
        self.current_holdings_detail = []

        selected = self.analyze_monthly_momentum(dates[0])
        self.selected_assets_history = [{'date': dates[0], 'assets': [a['name'] for a in selected] or ['Cash']}]

        for i in range(1, len(dates)):
            buy, sell = dates[i-1], dates[i]
            is_last = (i == len(dates) - 1)
            print(f"\n{'='*40}\n{sell[:7]} 월 수익률\n{'='*40}")
            ret = self.calculate_monthly_return(selected, buy, sell, pv, record_detail=is_last)
            pv *= (1 + ret)

            self.monthly_returns.append({
                'date': sell, 'return': ret, 'portfolio_value': pv, 'bok_rate': self.get_bok_rate(sell)
            })
            self.monthly_leverage_history.append({
                'date': sell, 'assets': [a['name'] for a in selected][:6] if selected else ['현금'], 'return': ret
            })
            print(f"월 수익률: {ret*100:+.2f}% | 포트폴리오: {pv:,.0f}원")

            selected = self.analyze_monthly_momentum(sell, show_detail=is_last)
            self.selected_assets_history.append({'date': sell, 'assets': [a['name'] for a in selected] or ['Cash']})

        self._print_current_holdings(dates[-2], dates[-1])
        self._print_final_results()

    def _print_final_results(self):
        if not self.monthly_returns:
            return
        df = pd.DataFrame(self.monthly_returns)
        df['cumulative_return'] = df['portfolio_value'] / self.initial_capital - 1

        fv = df['portfolio_value'].iloc[-1]
        tr = df['cumulative_return'].iloc[-1]
        n = len(df)
        # [R3] 마지막 구간이 부분월이어도 실제 경과일로 연환산
        years = (pd.Timestamp(df['date'].iloc[-1]) - pd.Timestamp(self._bt_start)).days / 365.25
        cagr = ((1 + tr) ** (1 / years) - 1) * 100 if years > 0 else 0
        excess = df['return'] - df['bok_rate'] / 100 / 12
        std = df['return'].std()
        sharpe = (excess.mean() / std) * np.sqrt(12) if std > 0 else 0
        df['dd'] = 1 - df['portfolio_value'] / df['portfolio_value'].cummax()
        mdd = max(df['dd'].max(), 1 - df['portfolio_value'].min() / self.initial_capital)
        wr = (df['return'] > 0).sum() / n * 100

        print(f"\n{'='*60}\n최종 결과\n{'='*60}")
        print(f"총 수익률: {tr*100:+.2f}% | CAGR: {cagr:+.2f}% ({years:.2f}년)")
        print(f"MDD(월말 기준): {mdd*100:.2f}% | 샤프(무위험 차감): {sharpe:.2f} | 승률: {wr:.1f}%")
        print(f"최종 자산: {fv:,.0f}원")
        print(f"거래비용 총: {self.total_transaction_costs:,.0f}원 ({self.total_transaction_costs/self.initial_capital*100:.2f}%)")
        print(f"조달비용 총(근사): {self.total_financing_costs:,.0f}원")
        print(f"손절: {len(self.stop_loss_history)}회 | 재진입: {len(self.reentry_history)}회 | "
              f"Trailing: {len(self.trailing_stop_history)}회")
        if n < 36:
            print(f"⚠ 표본 {n}개월: 통계적으로 전략 우위를 판단하기에 짧습니다 (REVIEW.md 참고).")

        self._print_monthly_stats(df)
        self._plot_results(df)

    def _print_monthly_stats(self, df):
        df['date'] = pd.to_datetime(df['date'])
        print(f"\n{'='*60}\n월별 수익률 추이\n{'='*60}")
        for _, r in df.iterrows():
            print(f"  {r['date'].strftime('%Y-%m')}: {r['return']*100:+.2f}% | {r['portfolio_value']:,.0f}원")
        best = df.loc[df['return'].idxmax()]
        worst = df.loc[df['return'].idxmin()]
        print(f"\n최고: {best['date'].strftime('%Y-%m')} ({best['return']*100:.2f}%)")
        print(f"최저: {worst['date'].strftime('%Y-%m')} ({worst['return']*100:.2f}%)")

    # ──────────────────────── 그래프 ────────────────────────

    @staticmethod
    def _set_korean_font():
        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in ("Malgun Gothic", "AppleGothic", "NanumGothic"):
            if name in available:
                plt.rcParams['font.family'] = name
                break
        plt.rcParams['axes.unicode_minus'] = False

    def _plot_results(self, df, save_path=None):
        if df.empty:
            return
        plt.style.use('seaborn-v0_8-darkgrid')
        self._set_korean_font()   # v1은 그린 뒤에 폰트를 지정해 '억/만' 눈금이 깨졌음
        fig, axes = plt.subplots(3, 1, figsize=(15, 12))
        df['date'] = pd.to_datetime(df['date'])

        ax = axes[0]
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda x, p: f'{x/1e8:.1f}억' if x >= 1e8 else f'{int(x/1e4)}만' if x >= 1e4 else f'{int(x)}'))
        ax.plot(df['date'], df['portfolio_value'], label='Portfolio', color='royalblue', lw=2)
        peak = df['portfolio_value'].cummax()
        ax.plot(df['date'], peak, '--', color='gray', alpha=0.7, label='Peak')
        ax.fill_between(df['date'], df['portfolio_value'], peak,
                        where=df['portfolio_value'] < peak, color='salmon', alpha=0.3)
        ax.set_title('Momentum Strategy - Hourly Trailing Stop')
        ax.legend()

        ax = axes[1]
        ax.bar(df['date'], df['return'] * 100, color=['green' if x > 0 else 'red' for x in df['return']],
               alpha=0.6, width=20)
        ax.axhline(0, color='black', lw=0.5)
        ax.set_title('Monthly Returns (%)')

        ax = axes[2]
        ax.plot(df['date'], df['bok_rate'], color='green', lw=2)
        ax.fill_between(df['date'], df['bok_rate'], alpha=0.3, color='green')
        ax.set_title('BOK Base Rate')

        plt.tight_layout()
        if save_path:
            fig.savefig(save_path, dpi=120)
        plt.show()

    # ──────────────────────── 결과 저장 ────────────────────────

    def save_results(self, prefix="momentum_hourly"):
        if not self.monthly_returns:
            return
        folder = Path.home() / "Downloads"
        if not folder.exists():
            folder = Path.cwd()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        datasets = [
            ('returns', self.monthly_returns),
            ('assets', self.selected_assets_history),
            ('transactions', self.transaction_history),
            ('stoploss', self.stop_loss_history),
            ('reentry', self.reentry_history),
            ('trailing', self.trailing_stop_history),
            ('leverage', self.monthly_leverage_history),
            ('current_holdings', self.current_holdings_detail),
        ]
        for name, data in datasets:
            if data:
                fn = folder / f"{prefix}_{name}_{ts}.csv"
                pd.DataFrame(data).to_csv(fn, index=False, encoding='utf-8-sig')
                print(f"✓ {name} 저장: {fn}")

    # ──────────────────────── 한국은행 기준금리 ────────────────────────

    @staticmethod
    def _build_bok_rates():
        """기준금리 변경 시점 테이블 (중간 월은 직전 값 유지).
        ※ 마지막 기록은 2025-05 인하(2.50%). 그 이후 금통위 결정은 이 코드에서 검증하지 않았습니다.
          실행 전 한국은행 '기준금리 추이' 페이지에서 확인하고, 변경이 있었다면 아래 목록에 추가하세요."""
        changes = [
            ("2000-12", 5.00), ("2001-07", 4.75), ("2001-08", 4.50), ("2001-09", 4.00),
            ("2002-05", 4.25), ("2003-05", 4.00), ("2003-07", 3.75), ("2004-08", 3.50),
            ("2004-11", 3.25), ("2005-10", 3.50), ("2005-12", 3.75), ("2006-02", 4.00),
            ("2006-06", 4.25), ("2006-08", 4.50), ("2007-07", 4.75), ("2007-08", 5.00),
            ("2008-08", 5.25), ("2008-10", 4.25), ("2008-11", 4.00), ("2008-12", 3.00),
            ("2009-01", 2.50), ("2009-02", 2.00), ("2010-07", 2.25), ("2010-11", 2.50),
            ("2011-01", 2.75), ("2011-03", 3.00), ("2011-06", 3.25),
            ("2012-07", 3.00), ("2012-10", 2.75), ("2013-05", 2.50),
            ("2014-08", 2.25), ("2014-10", 2.00), ("2015-03", 1.75), ("2015-06", 1.50),
            ("2016-06", 1.25), ("2017-11", 1.50), ("2018-11", 1.75),
            ("2019-07", 1.50), ("2019-10", 1.25), ("2020-03", 0.75), ("2020-05", 0.50),
            ("2021-08", 0.75), ("2021-11", 1.00), ("2022-01", 1.25), ("2022-04", 1.50),
            ("2022-05", 1.75), ("2022-07", 2.25), ("2022-08", 2.50), ("2022-10", 3.00),
            ("2022-11", 3.25), ("2023-01", 3.50), ("2024-10", 3.25), ("2024-11", 3.00),
            ("2025-02", 2.75), ("2025-05", 2.50),
        ]
        rates = {}
        months = pd.date_range("2000-12-01", datetime.now().strftime('%Y-%m') + "-01", freq='MS')
        change_dict = dict(changes)
        current_rate = 5.00
        for m in months:
            key = m.strftime('%Y-%m')
            current_rate = change_dict.get(key, current_rate)
            rates[key] = current_rate
        return rates

    # ──────────────────────── 초기화 출력 ────────────────────────

    def _print_init_info(self):
        lt = self.leverage_tiers
        print(f"--- 전략 초기화: {len(self.tickers_dict)}개 자산 ---")
        print(f"--- 포지션: 전체 ≤{self.max_positions}, 가상화폐 ≤{self.max_crypto_positions}, "
              f"비가상화폐 ≤{self.max_non_crypto_positions} ---")
        print(f"--- 3단계 레버리지: {lt[2][1]}x(≥{lt[2][0]}), {lt[1][1]}x(≥{lt[1][0]}), {lt[0][1]}x(≥{lt[0][0]}) ---")
        print(f"--- 손절: {self.stop_loss_pct*100:.1f}%, 재진입: +{self.reentry_threshold_pct*100:.1f}% "
              f"(최대 {self.max_reentry_count}회, 쿨다운 {self.reentry_cooldown_hours}h) ---")
        if self.enable_trailing_stop:
            tp = self.trailing_pct_map
            print(f"--- Trailing Stop: 가상화폐 {tp['crypto']*100:.1f}%, 채권 {tp['bond']*100:.1f}%, "
                  f"원자재 {tp['commodity']*100:.1f}%, 기본 {tp['default']*100:.1f}% ---")
        print(f"--- 거래비용: {self.transaction_cost*100:.2f}% | 1시간봉 UTC ---")


# ──────────────────────── 실행 ────────────────────────

if __name__ == "__main__":
    tickers = {
        "S&P 500": "449180.KS", "나스닥 종합": "449190.KS", "니케이 225": "241180.KS",
        "인도 Sensex": "453810.KS", "브라질 Bovespa": "EWZ", "FTSE 100": "^FTSE",
        "인도네시아 JSX": "EIDO", "독일 DAX": "411860.KS", "CSI300": "283580.KS",
        "KOSPI 200": "069500.KS", "홍콩 H지수": "099140.KS",
        "BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "금": "PAXG-USD",
        "미국 20년 국채 ETF": "TLT", "미국 리츠": "VNQ",
        "원유": "CL=F", "구리": "138910.KS",  # "달러 인덱스": "DX-Y.NYB",
        "글로벌 인프라": "IGF", "농산물": "DBA",
        "XRP/USD": "XRP-USD", "SOL/USD": "SOL-USD", "BNB/USD": "BNB-USD",
        "은": "SI=F", "TRX/USD": "TRX-USD", "코스닥150": "229200.KS",
    }

    BACKTEST_START = "2024-07-31"
    strategy = MomentumStrategy(
        tickers_dict=tickers, initial_capital=200000000,
        momentum_threshold_min=1.2, momentum_threshold_max=3.0,
        max_positions=10, max_crypto_positions=5, max_non_crypto_positions=5,
        base_leverage=1.0,
        leverage_threshold_1=1.2, leverage_multiplier_1=2.0,
        leverage_threshold_2=1.5, leverage_multiplier_2=1.0,
        leverage_threshold_3=2.0, leverage_multiplier_3=1.0,
        transaction_cost=0.001, sma_filter_months=6,
        macro_filter_ticker='^GSPC', macro_filter_sma_months=10,
        bond_filter_ticker='SHY', bond_filter_sma_months=10,
        stop_loss_pct=-0.03, reentry_threshold_pct=0.01,
        max_reentry_count=2, reentry_cooldown_hours=36,
        enable_trailing_stop=True,
        default_trailing_pct=0.03, crypto_trailing_pct=0.03,
        bond_trailing_pct=0.03, commodity_trailing_pct=0.03,
        # v1 결과와 직접 비교하려면 execute_next_session=False, financing_spread=None
        execute_next_session=True, financing_spread=0.02,
    )

    # [B5] history_start = 백테스트 시작일 → 다운로드 1회
    current_portfolio = strategy.analyze_current_portfolio(history_start=BACKTEST_START)
    strategy.run_backtest(BACKTEST_START, datetime.now().strftime('%Y-%m-%d'))
    strategy.save_results()
    if current_portfolio:
        pv_now = strategy.monthly_returns[-1]['portfolio_value'] if strategy.monthly_returns else strategy.initial_capital
        print("\n목표 포지션표 (주문 전송 안 함):")
        print(strategy.build_order_sheet(current_portfolio, pv_now).to_string(index=False))
