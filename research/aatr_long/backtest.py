"""AATR-L2X-S720 (Pine v6) 재현 백테스터와 TP·청산 SMA 변형 비교.

Pine 원본 규칙
  진입: 무포지션에서 4H 종가가 SMA720 + k*ATR20 상향 교차 (SMA720 > SMA720[30] 이면 k=0.75, 아니면 1.25)
  청산: 4H 종가가 청산 SMA 하향 교차
  체결: 신호 다음 봉 시가, 수량 = 자산 200% / 신호봉 종가, 수수료 0.1%/편도, 슬리피지 0
변형
  tp   : 평균진입가 * (1+tp) 지정가 익절. 체결 봉 종가에 주문이 걸려 다음 봉부터 유효,
         봉 고가가 닿으면 체결(시가가 이미 위면 시가 체결)
  exit : 청산 SMA 길이 (720 원본, 240 등). 'either' 는 exit 와 720 중 먼저 하향 교차하는 쪽
펀딩비는 Pine 이 계산하지 않으므로 기본값은 미반영, funding=True 면 자산에서 차감한다.
"""
from pathlib import Path
import json, glob
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
H4 = 14400000


def load(symbol):
    rows = []
    for p in sorted(glob.glob(str(ROOT / f'data/raw/{symbol}_*_4h.json'))):
        rows += json.loads(Path(p).read_text())['rows']
    d = pd.DataFrame([r[:5] for r in rows], columns=['time', 'open', 'high', 'low', 'close'])
    d = d.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    for k in ['open', 'high', 'low', 'close']:
        d[k] = d[k].astype(float)
    if d.time.diff().dropna().ne(H4).any():
        raise ValueError(f'{symbol}: missing 4h candle')
    fr = []
    for p in sorted(glob.glob(str(ROOT / f'data/raw/{symbol}_*_funding.json'))):
        fr += json.loads(Path(p).read_text())['rows']
    f = pd.DataFrame(fr)
    f['t'] = (f.fundingTime.astype('int64') // 60000) * 60000
    f['rate'] = f.fundingRate.astype(float)
    f = f.drop_duplicates('t').sort_values('t')
    # 펀딩 정산 시각이 속한 4H 봉 인덱스 (정산 시점 가격은 그 봉 시가로 근사)
    idx = np.searchsorted(d.time.to_numpy(), f.t.to_numpy(), side='right') - 1
    fund = np.zeros(len(d))
    np.add.at(fund, idx[idx >= 0], f.rate.to_numpy()[idx >= 0])
    d['fund'] = fund
    return d


def rma(x, n):
    out = np.full(len(x), np.nan)
    out[n - 1] = x[:n].mean()
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def indicators(d, entry_len=720, atr_len=20, slope=30, k_up=0.75, k_dn=1.25):
    c, h, l = d.close.to_numpy(), d.high.to_numpy(), d.low.to_numpy()
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l, np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)]))
    sma = pd.Series(c).rolling(entry_len).mean().to_numpy()
    atr = rma(tr, atr_len)
    rising = sma > np.r_[np.full(slope, np.nan), sma[:-slope]]
    band = sma + np.where(rising, k_up, k_dn) * atr
    return sma, band


def cross_over(a, b):
    return (a > b) & (np.r_[np.nan, a[:-1]] <= np.r_[np.nan, b[:-1]])


def cross_under(a, b):
    return (a < b) & (np.r_[np.nan, a[:-1]] >= np.r_[np.nan, b[:-1]])


def run(d, tp=None, exit_len=720, either=False, start=None, end=None,
        lev=2.0, fee=0.001, cap=100000.0, funding=False):
    o, h, l, c = (d[k].to_numpy() for k in ['open', 'high', 'low', 'close'])
    t, fund = d.time.to_numpy(), d.fund.to_numpy()
    sma720, band = indicators(d)
    ent = cross_over(c, band)
    xs = cross_under(c, pd.Series(c).rolling(exit_len).mean().to_numpy())
    if either:
        xs = xs | cross_under(c, sma720)
    n = len(d)
    lo = 0 if start is None else int(np.searchsorted(t, start))
    hi = n if end is None else int(np.searchsorted(t, end))
    cash, qty, avg = cap, 0.0, 0.0
    tp_px = np.nan
    pend_entry = pend_exit = False
    funding_paid = 0.0
    eq_close = np.empty(hi - lo); eq_low = np.empty(hi - lo)
    trades = []; cur = None
    for j, i in enumerate(range(lo, hi)):
        # 1) 시가: 직전 봉 신호의 시장가 주문
        if pend_exit and qty > 0:
            cash += qty * o[i] * (1 - fee)
            cur.update(exit_t=t[i], exit_px=o[i], reason='SMA')
            cur['pnl'] = cash - cur['eq0']; trades.append(cur); cur = None
            qty = 0.0; tp_px = np.nan
        if pend_entry and qty == 0:
            qty = pend_entry
            avg = o[i]
            cash -= qty * o[i] * (1 + fee)
            cur = dict(entry_t=t[i], entry_px=o[i], eq0=cash + qty * o[i] * (1 + fee), qty=qty, fund=0.0)
            filled_now = True
        else:
            filled_now = False
        pend_entry = pend_exit = False
        # 2) 펀딩: 누적은 항상, 자산 반영은 funding=True 일 때만
        if qty > 0 and fund[i] != 0 and not filled_now:
            fcost = qty * o[i] * fund[i]
            funding_paid += fcost; cur['fund'] += fcost
            if funding:
                cash -= fcost
        # 3) 장중 익절 지정가 (체결 봉 다음 봉부터)
        if qty > 0 and not np.isnan(tp_px) and h[i] >= tp_px:
            px = max(o[i], tp_px)
            cash += qty * px * (1 - fee)
            cur.update(exit_t=t[i], exit_px=px, reason='TP')
            cur['pnl'] = cash - cur['eq0']; trades.append(cur); cur = None
            qty = 0.0; tp_px = np.nan
        eq_close[j] = cash + qty * c[i]
        eq_low[j] = cash + qty * l[i]
        # 4) 종가 확정: 신호 판정
        if qty > 0:
            if tp is not None and np.isnan(tp_px):
                tp_px = avg * (1 + tp)
            if xs[i]:
                pend_exit = True
        elif ent[i] and i + 1 < hi:
            pend_entry = lev * eq_close[j] / c[i]
    if qty > 0:  # 기간 끝 미청산: 마지막 종가로 평가
        cur.update(exit_t=t[hi - 1], exit_px=c[hi - 1], reason='OPEN')
        cur['pnl'] = eq_close[-1] - cur['eq0']; trades.append(cur)
    return metrics(t[lo:hi], eq_close, eq_low, trades, cap, funding_paid), trades, eq_close


def metrics(t, eq, eql, trades, cap, funding_paid):
    years = (t[-1] - t[0]) / (365.25 * 86400000)
    peak = np.maximum.accumulate(eq)
    mdd = (eq / peak - 1).min()
    mdd_low = (eql / np.maximum.accumulate(np.maximum(eq, eql)) - 1).min()
    pnl = np.array([x['pnl'] for x in trades]) if trades else np.array([0.0])
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    ret = eq[-1] / cap - 1
    cagr = (eq[-1] / cap) ** (1 / years) - 1 if eq[-1] > 0 else -1
    daily = pd.Series(eq, index=pd.to_datetime(t, unit='ms')).resample('1D').last().pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(365) if daily.std() > 0 else np.nan
    held = sum((x['exit_t'] - x['entry_t']) for x in trades) / (t[-1] - t[0] + H4) if trades else 0
    tp_n = sum(1 for x in trades if x['reason'] == 'TP')
    return dict(ret=ret, cagr=cagr, mdd=mdd, mdd_intrabar=mdd_low, mar=cagr / -mdd if mdd < 0 else np.nan,
                sharpe=sharpe, trades=len(trades), tp_exits=tp_n,
                win=float((pnl > 0).mean()) if trades else 0, pf=wins / losses if losses > 0 else np.inf,
                exposure=held, funding_pct_of_cap=funding_paid / cap)


VARIANTS = {
    'A 원본 (S720 청산)': dict(),
    'B +TP15': dict(tp=0.15),
    'C S240 청산': dict(exit_len=240),
    'D TP15 + S240': dict(tp=0.15, exit_len=240),
    'E S240 또는 S720': dict(exit_len=240, either=True),
    'F TP15 + (S240 또는 S720)': dict(tp=0.15, exit_len=240, either=True),
}
