"""합성 데이터로 v2 수정 사항을 검증 (네트워크 불필요).
실행: python -m unittest global_macro_momentum/test_momentum_strategy.py
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ.setdefault("MPLBACKEND", "Agg")
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

import momentum_strategy as ms

KS, US, CR = "KS", "US", "CR"


def _market(t):
    if t.endswith(".KS"):
        return KS
    if t.endswith("-USD"):
        return CR
    return US


def make_hourly(tickers, start, end, drift=None, vol=0.004, seed=0, shocks=None):
    """시장별 거래시간을 흉내 낸 1시간봉 (UTC, 봉 시작 시각 라벨).
    shocks: {ticker: [(utc_ts_str, multiplier_from_then_on)]}"""
    rng = np.random.default_rng(seed)
    all_h = pd.date_range(start, end, freq="h", tz="UTC")
    out = {}
    for t in tickers:
        m = _market(t)
        if m == CR:
            idx = all_h
        elif m == KS:
            idx = all_h[(all_h.dayofweek < 5) & (all_h.hour < 7)]
        else:
            idx = all_h[(all_h.dayofweek < 5) & (all_h.hour >= 13) & (all_h.hour <= 19)] + pd.Timedelta(minutes=30)
            idx = idx[idx.hour <= 19]
        mu = (drift or {}).get(t, 0.0)
        steps = rng.normal(mu, vol, len(idx)) if vol else np.full(len(idx), mu)
        px = 100 * np.exp(np.cumsum(steps))
        s = pd.Series(px, index=idx)
        for ts, mult in (shocks or {}).get(t, []):
            s.loc[s.index >= pd.Timestamp(ts, tz="UTC")] *= mult
        out[t] = s
    return out


def daily_from_hourly(hourly):
    """일봉 날짜 D = 해당 UTC 날짜의 마지막 시간봉 종가 (크립토 UTC 일봉과 동일한 정의)"""
    out = {}
    for t, s in hourly.items():
        d = s.groupby(s.index.tz_convert(None).normalize()).last()
        out[t] = d
    return out


def fake_download_factory(hourly, hourly_available_from):
    daily = daily_from_hourly(hourly)

    def _frame(series_map, tickers):
        df = pd.DataFrame({t: series_map[t] for t in tickers if t in series_map})
        df.columns = pd.MultiIndex.from_product([["Close"], df.columns])
        return df

    def fake_download(tickers, start=None, end=None, interval="1d", **kw):
        if interval == "1h":
            lo = max(pd.Timestamp(start), pd.Timestamp(hourly_available_from)).tz_localize("UTC")
            hi = pd.Timestamp(end).tz_localize("UTC")
            sm = {t: s[(s.index >= lo) & (s.index < hi)] for t, s in hourly.items()}
            return _frame(sm, tickers)
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        sm = {t: s[(s.index >= lo) & (s.index < hi)] for t, s in daily.items()}
        return _frame(sm, tickers)

    return fake_download


FILTERS = ["^GSPC", "SHY"]


def build(tickers_dict, hourly, hourly_from, **kw):
    params = dict(tickers_dict=tickers_dict, initial_capital=100_000_000,
                  base_leverage=1.0, leverage_multiplier_1=1.0, leverage_multiplier_2=1.0,
                  leverage_multiplier_3=1.0, transaction_cost=0.001,
                  stop_loss_pct=-0.03, reentry_threshold_pct=0.01, max_reentry_count=2,
                  reentry_cooldown_hours=36, default_trailing_pct=0.03, crypto_trailing_pct=0.03,
                  bond_trailing_pct=0.03, commodity_trailing_pct=0.03,
                  macro_filter_sma_months=10, bond_filter_sma_months=10)
    params.update(kw)
    with redirect_stdout(io.StringIO()):
        s = ms.MomentumStrategy(**params)
    s._fake = fake_download_factory(hourly, hourly_from)
    return s


def run_quiet(fn, *a, **k):
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


class PrepMixin:
    def prepare(self, s, start, end):
        with mock.patch.object(ms.yf, "download", side_effect=s._fake), \
             mock.patch.object(ms, "datetime", wraps=ms.datetime) as dt:
            dt.now.return_value = pd.Timestamp(end).to_pydatetime()
            dt.strptime = ms.datetime.strptime
            run_quiet(s._prepare_data, start, end)


class TestMonitoringWindow(unittest.TestCase, PrepMixin):
    def setUp(self):
        self.tick = {"BTC/USD": "BTC-USD"}
        self.all = list(self.tick.values()) + FILTERS

    def _single_month(self, shocks, **kw):
        hourly = make_hourly(self.all, "2023-01-01", "2024-03-05", vol=0.0, shocks=shocks,
                             drift={"BTC-USD": 0.0})
        s = build(self.tick, hourly, "2022-01-01", **kw)
        self.prepare(s, "2024-01-31", "2024-03-04")
        s.is_risk_on = True
        sel = [{"name": "BTC/USD", "ticker": "BTC-USD", "momentum_score": 1.3, "target_weight": 0.5}]
        ret, _ = run_quiet(s.calculate_monthly_return, sel, "2024-01-31", "2024-02-29", 1e8)
        return s, ret

    def test_pre_entry_dip_does_not_trigger_stop(self):
        # [B1] 진입(1/31 종가 = 2/1 00:00 UTC) 이전인 1/31 10:00에 -10% 급락 후 22:00에 원복.
        #      v1은 1/31 00:00부터 감시해서 여기서 손절이 났음.
        shocks = {"BTC-USD": [("2024-01-31 10:00", 0.9), ("2024-01-31 22:00", 1 / 0.9)]}
        s, _ = self._single_month(shocks, execute_next_session=False, financing_spread=None)
        self.assertEqual(len(s.stop_loss_history), 0)

    def test_last_day_is_monitored(self):
        # [B1] 청산일(2/29) 당일 급락은 감시되어야 함. v1은 2/29 00:00에서 감시를 끝냈음.
        shocks = {"BTC-USD": [("2024-02-29 12:00", 0.9)]}
        s, _ = self._single_month(shocks, execute_next_session=False, financing_spread=None)
        self.assertEqual(len(s.stop_loss_history), 1)
        self.assertEqual(s.stop_loss_history[0]["date"], pd.Timestamp("2024-02-29 12:00", tz="UTC"))

    def test_flat_price_return_is_cost_plus_cash(self):
        s, ret = self._single_month({}, execute_next_session=False, financing_spread=None)
        rate = s.get_bok_rate("2024-02-29") / 100
        hours = 29 * 24
        pos = (1 - 0.001) / (1 + 0.001)                       # 왕복 비용
        cash = 1 + rate * hours / ms.HOURS_PER_YEAR            # 가상화폐 단독 → 50% 현금
        self.assertAlmostEqual(ret, 0.5 * pos + 0.5 * cash - 1, places=10)

    def test_financing_cost_applied_for_leverage(self):
        kw = dict(execute_next_session=False, leverage_multiplier_1=2.0)
        _, r_no = self._single_month({}, financing_spread=None, **kw)
        _, r_fin = self._single_month({}, financing_spread=0.02, **kw)
        self.assertLess(r_fin, r_no)


class TestPartialHourlyCoverage(unittest.TestCase, PrepMixin):
    def test_gap_before_hourly_start_is_monitored_with_daily(self):
        # [B2] 시간봉이 2/20부터만 있는 달. 2/5 급락은 일봉으로 감시돼야 함 (v1은 감시 안 함).
        tick = {"BTC/USD": "BTC-USD"}
        hourly = make_hourly(list(tick.values()) + FILTERS, "2023-01-01", "2024-03-05", vol=0.0,
                             shocks={"BTC-USD": [("2024-02-05 12:00", 0.9)]})
        s = build(tick, hourly, "2024-02-20", execute_next_session=False, financing_spread=None)
        self.prepare(s, "2024-01-31", "2024-03-04")
        self.assertGreaterEqual(s.hourly_price_data.index.min(), pd.Timestamp("2024-02-20", tz="UTC"))
        s.is_risk_on = True
        sel = [{"name": "BTC/USD", "ticker": "BTC-USD", "momentum_score": 1.3, "target_weight": 0.5}]
        run_quiet(s.calculate_monthly_return, sel, "2024-01-31", "2024-02-29", 1e8)
        self.assertEqual(len(s.stop_loss_history), 1)
        # 2/5 종가는 2/6 00:00 UTC에 확정 → 그 시각에 체크되어야 (미래참조 없음)
        self.assertEqual(s.stop_loss_history[0]["date"], pd.Timestamp("2024-02-06", tz="UTC"))


class TestNoLookahead(unittest.TestCase, PrepMixin):
    def test_sma_ignores_data_after_end_date(self):
        tick = {"BTC/USD": "BTC-USD"}
        hourly = make_hourly(list(tick.values()) + FILTERS, "2023-01-01", "2024-03-31", vol=0.0,
                             shocks={"BTC-USD": [("2024-03-20", 5.0)]})
        s = build(tick, hourly, "2022-01-01")
        self.prepare(s, "2024-01-31", "2024-03-30")
        sma = s._calculate_monthly_sma("BTC-USD", "2024-03-10", 6)
        self.assertAlmostEqual(sma, 100.0, places=6)   # v1은 3/31 값(500)을 섞어 SMA를 올렸음


class TestSelection(unittest.TestCase):
    def test_max_positions_enforced(self):
        with redirect_stdout(io.StringIO()):
            s = ms.MomentumStrategy({}, max_positions=3, max_crypto_positions=5, max_non_crypto_positions=5)
        q = [{"name": n, "ticker": n, "momentum_score": 2.0 - i * 0.1}
             for i, n in enumerate(["BTC/USD", "금", "ETH/USD", "원유", "SOL/USD"])]
        out = s._apply_crypto_weight_limit(q)
        self.assertEqual([a["name"] for a in out], ["BTC/USD", "ETH/USD", "금"])
        self.assertAlmostEqual(sum(a["target_weight"] for a in out), 1.0)


class TestEndToEnd(unittest.TestCase, PrepMixin):
    def test_full_backtest_runs(self):
        tick = {"S&P 500": "449180.KS", "KOSPI 200": "069500.KS", "미국 20년 국채 ETF": "TLT",
                "원유": "CL=F", "BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD"}
        tickers = list(tick.values()) + FILTERS
        drift = {t: 0.00008 for t in tickers}
        drift["SHY"] = 0.00001
        hourly = make_hourly(tickers, "2022-06-01", "2024-12-31", drift=drift, vol=0.004, seed=7)
        for nxt in (False, True):
            s = build(tick, hourly, "2024-06-01", execute_next_session=nxt, financing_spread=0.02,
                      leverage_multiplier_1=2.0)
            self.prepare(s, "2023-08-31", "2024-12-30")
            with mock.patch.object(ms.plt, "show"):
                run_quiet(s.run_backtest, "2023-08-31", "2024-12-30")
            self.assertEqual(len(s.monthly_returns), 16)
            vals = [m["portfolio_value"] for m in s.monthly_returns]
            self.assertTrue(all(np.isfinite(vals)) and all(v > 0 for v in vals))
            # 재진입은 항상 먼저 손절이 있어야 하고, 한 달 한도를 넘지 않아야 함
            per_month = pd.Series([r["month"] + r["ticker"] for r in s.reentry_history]).value_counts()
            self.assertTrue((per_month <= s.max_reentry_count).all() if len(per_month) else True)
            self.assertLessEqual(len(s.reentry_history), len(s.stop_loss_history))


if __name__ == "__main__":
    unittest.main()
