"""TP15 · SMA240 청산 도입 효과 검증 실행. 결과는 research/aatr_long/results/ 에 저장."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest import load, run, VARIANTS  # noqa: E402

OUT = Path(__file__).parent / 'results'; OUT.mkdir(exist_ok=True)
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
           'DOGEUSDT', 'LINKUSDT', 'LTCUSDT', 'BCHUSDT', 'AVAXUSDT']
# SMA720(120일)+기울기 30봉 예열이 끝난 뒤부터 평가
START = int(pd.Timestamp('2021-03-08', tz='UTC').timestamp() * 1000)
SPLIT = int(pd.Timestamp('2024-01-01', tz='UTC').timestamp() * 1000)

data = {s: load(s) for s in SYMBOLS}

# 1) 6개 변형 × 11개 종목, 전체 기간
rows = []
for s, d in data.items():
    bh = d.close[d.time >= START]
    for name, kw in VARIANTS.items():
        m, tr, _ = run(d, start=START, **kw)
        rows.append(dict(symbol=s, variant=name, bh=bh.iloc[-1] / bh.iloc[0] - 1, **m))
full = pd.DataFrame(rows)
full.to_csv(OUT / 'variants_by_symbol.csv', index=False)

# 1b) 펀딩비 반영 (Pine 에는 없는 실거래 비용)
rows = []
for s, d in data.items():
    for name, kw in VARIANTS.items():
        m, _, _ = run(d, start=START, funding=True, **kw)
        rows.append(dict(symbol=s, variant=name, **m))
fund = pd.DataFrame(rows)
fund.to_csv(OUT / 'variants_by_symbol_funding.csv', index=False)

# 2) 기간 분할 (2021-03~2023 / 2024~2026-08) — 과최적화 점검
rows = []
for s, d in data.items():
    for name, kw in VARIANTS.items():
        for label, a, b in [('2021-03~2023', START, SPLIT), ('2024~2026-08', SPLIT, None)]:
            m, _, _ = run(d, start=a, end=b, **kw)
            rows.append(dict(symbol=s, variant=name, period=label, **m))
split = pd.DataFrame(rows)
split.to_csv(OUT / 'variants_by_period.csv', index=False)

# 3) 이웃 파라미터 격자: TP × 청산 SMA (TP15·S240 이 고립된 봉우리인지)
tps = [None, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40]
exits = [120, 180, 240, 360, 480, 720]
rows = []
for s, d in data.items():
    for tp in tps:
        for ex in exits:
            m, _, _ = run(d, start=START, tp=tp, exit_len=ex)
            rows.append(dict(symbol=s, tp='없음' if tp is None else f'{int(tp*100)}%', exit_sma=ex, **m))
grid = pd.DataFrame(rows)
grid.to_csv(OUT / 'grid_tp_exit.csv', index=False)

# 4) BTC 연도별
rows = []
d = data['BTCUSDT']
for name, kw in VARIANTS.items():
    _, _, eq = run(d, start=START, **kw)
    t = pd.to_datetime(d.time[d.time >= START].to_numpy(), unit='ms')
    y = pd.Series(eq, index=t).resample('YE').last()
    prev = pd.concat([pd.Series([100000.0]), y.iloc[:-1]]).to_numpy()
    for yr, v, p in zip(y.index.year, y.to_numpy(), prev):
        rows.append(dict(variant=name, year=yr, ret=v / p - 1))
pd.DataFrame(rows).to_csv(OUT / 'btc_yearly.csv', index=False)

# 5) BTC 거래 내역
for name, kw in [('A', {}), ('D', dict(tp=0.15, exit_len=240))]:
    _, tr, _ = run(data['BTCUSDT'], start=START, **kw)
    t = pd.DataFrame(tr)
    for k in ['entry_t', 'exit_t']:
        t[k] = pd.to_datetime(t[k], unit='ms')
    t['ret_on_eq'] = t.pnl / t.eq0
    t.to_csv(OUT / f'btc_trades_{name}.csv', index=False)

pd.set_option('display.width', 250); pd.set_option('display.max_columns', 30)
fmt = lambda x: f'{x:,.2f}'
print(full[full.symbol == 'BTCUSDT'].drop(columns='symbol').to_string(index=False, float_format=fmt))
print('\n# BTC 펀딩비 반영')
print(fund[fund.symbol == 'BTCUSDT'][['variant', 'ret', 'cagr', 'mdd', 'mar', 'sharpe']].to_string(index=False, float_format=fmt))
print('\n# 종목 중앙값')
print(full.groupby('variant', sort=False)[['ret', 'cagr', 'mdd', 'mar', 'sharpe', 'trades', 'win', 'pf', 'exposure']]
      .median().to_string(float_format=fmt))
print('\n# 원본 대비 MAR 개선 종목 수 / CAGR 개선 종목 수')
base = full[full.variant.str.startswith('A')].set_index('symbol')
for v in VARIANTS:
    x = full[full.variant == v].set_index('symbol')
    print(v, int((x.mar > base.mar).sum()), int((x.cagr > base.cagr).sum()), '/ 11')
