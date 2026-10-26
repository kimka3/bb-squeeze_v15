"""BTC vs ETH: 같은 원본 규칙에서 성과가 갈리는 원인 분해 (4H 레포 데이터, 2021-03-08 ~ 2026-08-31)."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest import load, run, indicators, cross_under  # noqa: E402

START = int(pd.Timestamp('2021-03-08', tz='UTC').timestamp() * 1000)
rows = []
for sym in ['BTCUSDT', 'ETHUSDT']:
    d = load(sym)
    sma, band = indicators(d)
    m, tr, eq = run(d, start=START)
    t = d.time.to_numpy(); c = d.close.to_numpy(); h = d.high.to_numpy(); l = d.low.to_numpy()
    lo = int(np.searchsorted(t, START))
    tt = pd.DataFrame(tr)
    tt['r'] = tt.pnl / tt.eq0
    # 각 거래: 진입가 대비 최고가(MFE), 청산가, 진입 시 SMA 와의 거리, 청산 시 SMA 와의 거리
    mfe, exit_vs_sma, entry_vs_sma, bars = [], [], [], []
    for _, x in tt.iterrows():
        i0, i1 = np.searchsorted(t, x.entry_t), np.searchsorted(t, x.exit_t)
        mfe.append(h[i0:i1 + 1].max() / x.entry_px - 1)
        entry_vs_sma.append(x.entry_px / sma[i0] - 1)
        exit_vs_sma.append(x.exit_px / sma[i1] - 1)
        bars.append(i1 - i0)
    tt['mfe'] = mfe; tt['px_ret'] = tt.exit_px / tt.entry_px - 1
    tt['entry_vs_sma'] = entry_vs_sma; tt['exit_vs_sma'] = exit_vs_sma; tt['bars'] = bars
    win, loss = tt[tt.r > 0], tt[tt.r <= 0]
    seg = slice(lo, len(d))
    ret4h = np.diff(np.log(c[seg]))
    above = c[seg] > sma[seg]
    flips = int(np.sum(above[1:] != above[:-1]))
    big = tt.r.sort_values(ascending=False)
    rows.append({
        '종목': sym,
        'CAGR(2x)': m['cagr'], 'MDD': m['mdd'], '거래': len(tt), '승률': len(win) / len(tt),
        'PF': m['pf'], '평균 이익(자산%)': win.r.mean(), '평균 손실(자산%)': loss.r.mean(),
        '이익/손실 비': win.r.mean() / -loss.r.mean(),
        '손실거래 평균 보유봉': loss.bars.mean(),
        '진입가-SMA 거리(가격%)': tt.entry_vs_sma.mean(),
        '청산가-SMA 거리(가격%)': tt.exit_vs_sma.mean(),
        '승리거래 MFE 평균(가격%)': win.mfe.mean(), '승리거래 실현(가격%)': win.px_ret.mean(),
        'MFE 반납률': 1 - (win.px_ret / win.mfe).mean(),
        '최대 1건 비중(순이익)': tt.pnl.max() / tt.pnl.sum() if tt.pnl.sum() > 0 else np.nan,
        '상위3건 자산배수': float(np.prod(1 + big.iloc[:3])),
        '나머지 자산배수': float(np.prod(1 + big.iloc[3:])),
        '연 변동성(4H)': ret4h.std() * np.sqrt(6 * 365),
        'ATR/가격 평균': float(np.nanmean((band[seg] - sma[seg]) / sma[seg])),
        'SMA720 위 비중': above.mean(), 'SMA720 교차 횟수': flips,
        'BH 수익': c[-1] / c[lo] - 1,
    })
    tt[['entry_t', 'exit_t', 'entry_px', 'exit_px', 'r', 'mfe', 'px_ret', 'entry_vs_sma', 'exit_vs_sma', 'bars']].to_csv(
        Path(__file__).parent / 'results' / f'why_{sym}_trades.csv', index=False)
pd.set_option('display.width', 200)
print(pd.DataFrame(rows).set_index('종목').T.to_string(float_format=lambda x: f'{x:,.3f}'))
