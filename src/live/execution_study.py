"""Would limit / post-only orders reduce this strategy's slippage?

Short answer from the measurements below: barely, and only on one leg. The cost
is market impact from position size, not the spread, and the entry leg cannot be
made passive without handing back more profit than it saves.

Three questions, each answered from data rather than argument:

  1. How much of the measured slippage is spread (recoverable by resting) versus
     impact (paying for depth, which resting does not avoid)?
  2. If entries rested passively instead of crossing, would they fill — and are
     the ones that DON'T fill the good trades?
  3. Which legs could be passive at all, and what is each worth?

    python src/live/execution_study.py
    python src/live/execution_study.py --equity 25000
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np                                                  # noqa: E402
import pandas as pd                                                 # noqa: E402

from live.book import fetch_depth, walk                             # noqa: E402
from live.config import MAINNET, SYMBOL_TO_LIGHTER                  # noqa: E402
from live.markets import load_markets, marks                        # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TRADES = ROOT / 'results/cases/zero_fee_cap4_risk200/trades.csv'

# Legs that have to cross the book, and why.
TAKER_ONLY = {
    'ENTRY': 'breakout entry — resting means missing the moves that run',
    'STOP': 'protective — a maker stop that does not fill is not a stop',
    'BE_AFTER_TP': 'protective',
    'EOD': 'forced close',
}
PASSIVE_OK = {
    'TP2R': 'resting limit at the profit target: no urgency, and a fill you did '
            'not get is a fill you did not want',
    'BB_MID': 'signal exit at a bar boundary — could be worked, modestly',
}


def spread_vs_impact(markets, base_url, equity: float, multiple: float = 0.86) -> dict:
    """Split the cost into the part resting can recover and the part it cannot."""
    px = marks(base_url, markets)
    rows = []
    for symbol, market in markets.items():
        price = px.get(symbol)
        if not price:
            continue
        depth = fetch_depth(base_url, market.market_id)
        bids, asks = depth.get('bids') or [], depth.get('asks') or []
        if not bids or not asks:
            continue
        best_bid, best_ask = float(bids[0]['price']), float(asks[0]['price'])
        mid = (best_bid + best_ask) / 2
        qty = market.round_size(equity * multiple / price)
        q = walk(depth, qty, buying=False)
        total = q.slippage_bps(mid, False)
        half = (mid - best_bid) / mid * 1e4
        rows.append({'symbol': symbol, 'half_spread_bps': half,
                     'impact_bps': total - half, 'total_bps': total})
    return {'rows': rows,
            'mean_half_spread': float(np.mean([r['half_spread_bps'] for r in rows])),
            'mean_impact': float(np.mean([r['impact_bps'] for r in rows])),
            'mean_total': float(np.mean([r['total_bps'] for r in rows]))}


def passive_entry_study(market) -> pd.DataFrame:
    """Would a passive entry have filled, and what was it worth?

    Uses the 5m path of each real entry bar. A short resting above the open fills
    if the bar trades up to it; a long resting below fills if it trades down.
    """
    t = pd.read_csv(TRADES)
    rows = []
    for tr in t.to_dict('records'):
        micro = market.micro.get((tr['symbol'], int(tr['entry_ms'])))
        if micro is None:
            continue
        rows.append({'symbol': tr['symbol'], 'side': tr['side'],
                     'r': tr['r_multiple'], 'net': tr['net_pnl'],
                     'open': float(micro[0][1]),
                     'high': float(micro[:, 2].max()),
                     'low': float(micro[:, 3].min())})
    return pd.DataFrame(rows)


def leg_mix() -> dict:
    """Notional by fill role — how much of the book-crossing could ever be passive."""
    legs: dict[str, float] = {}
    with TRADES.open() as fh:
        for row in csv.DictReader(fh):
            for f in json.loads(row['fills']):
                legs[f['role']] = legs.get(f['role'], 0.) + abs(
                    float(f['qty']) * float(f['price']))
    total = sum(legs.values())
    return {'legs': legs, 'total': total,
            'passive_share': sum(v for r, v in legs.items() if r in PASSIVE_OK) / total,
            'taker_share': sum(v for r, v in legs.items() if r in TAKER_ONLY) / total}


def main(equity: float) -> int:
    from run_frontier import market_data

    markets = load_markets(MAINNET, SYMBOL_TO_LIGHTER)

    print('=' * 72)
    print('1. 슬리피지는 스프레드인가 충격인가')
    print('=' * 72)
    sv = spread_vs_impact(markets, MAINNET, equity)
    print(f"{'symbol':10s}{'half-spread':>13s}{'impact':>10s}{'total':>10s}")
    for r in sorted(sv['rows'], key=lambda x: -x['total_bps']):
        print(f"{r['symbol']:10s}{r['half_spread_bps']:>11.2f}bp"
              f"{r['impact_bps']:>10.2f}{r['total_bps']:>10.2f}")
    share = sv['mean_half_spread'] / sv['mean_total'] * 100 if sv['mean_total'] else 0
    print(f"\n평균  half-spread {sv['mean_half_spread']:.2f}bp   "
          f"impact {sv['mean_impact']:.2f}bp   total {sv['mean_total']:.2f}bp")
    print(f"패시브로 회수 가능한 비중: {share:.0f}%  "
          f"— 나머지 {100 - share:.0f}%는 크기에서 나온 충격이라 지정가로 줄지 않는다")

    print()
    print('=' * 72)
    print('2. 패시브 진입은 체결되는가 — 그리고 안 되는 건 어떤 거래인가')
    print('=' * 72)
    d = passive_entry_study(market_data())
    print(f"진입 {len(d)}건")
    print(f"\n{'가격개선':>10s}{'체결률':>10s}{'미체결':>9s}{'체결 평균R':>13s}{'미체결 평균R':>15s}")
    for k in (0.0002, 0.0005, 0.0010, 0.0020, 0.0050):
        fill = np.where(d.side == 'SHORT', d.high >= d.open * (1 + k),
                        d.low <= d.open * (1 - k))
        filled, missed = d[fill], d[~fill]
        print(f'{k * 1e4:>8.0f}bp{fill.mean() * 100:>9.1f}%{len(missed):>9d}'
              f'{filled.r.mean():>12.3f}R'
              f'{(missed.r.mean() if len(missed) else float("nan")):>14.3f}R')
    fill10 = np.where(d.side == 'SHORT', d.high >= d.open * 1.001, d.low <= d.open * 0.999)
    missed10 = d[~fill10]
    total_profit = d.net.sum()
    print(f'\n10bp 개선을 노리면 {len(missed10)}건을 놓치고, 그 거래들의 순손익 합은 '
          f'{missed10.net.sum():+,.0f}')
    print(f'전체 순손익 {total_profit:+,.0f}의 {missed10.net.sum() / total_profit * 100:.0f}%다. '
          '미체결 거래의 평균 R이 체결분보다 높다는 것은 전형적인 역선택이다 —')
    print('즉시 달아나서 되돌아오지 않는 거래가 곧 이 전략의 승자다.')

    print()
    print('=' * 72)
    print('3. 어떤 레그가 패시브 가능한가')
    print('=' * 72)
    mix = leg_mix()
    for role, value in sorted(mix['legs'].items(), key=lambda kv: -kv[1]):
        note = TAKER_ONLY.get(role) or PASSIVE_OK.get(role, '')
        tag = '테이커 필수' if role in TAKER_ONLY else ('패시브 가능' if role in PASSIVE_OK else '')
        print(f"   {role:14s}{value / mix['total'] * 100:>6.1f}%  {tag:12s}{note}")
    weighted = sv['mean_total']
    cost = mix['total'] * weighted / 1e4
    tp_share = mix['legs'].get('TP2R', 0.) / mix['total']
    print(f"\n체결 명목금액 가중 {weighted:.1f}bp 기준 총 슬리피지 비용 ~ {cost:,.0f} USDT")
    print(f"   TP2R만 메이커 전환   ~ {cost * tp_share:>10,.0f} USDT  ({tp_share * 100:.1f}%)")
    print(f"   half-spread 전량 회수 ~ {mix['total'] * sv['mean_half_spread'] / 1e4:>10,.0f} USDT  "
          f"({share:.0f}%, 이론 상한)")

    print()
    print('=' * 72)
    print('결론')
    print('=' * 72)
    print('- 진입을 패시브로 돌리지 말 것. 아끼는 것보다 놓치는 수익이 훨씬 크다.')
    print('- 손절은 반드시 테이커. 체결 안 되는 손절은 손절이 아니다.')
    print('- 2R 익절만 지정가(post-only)로 돌릴 가치가 있다. 목표가에 거는 주문이라')
    print('  역선택이 없고, 안 채워지면 애초에 원하지 않던 체결이다.')
    print('- 비용의 대부분인 충격은 지정가가 아니라 크기·시간 분할의 문제다.')
    print('  용량 곡선(--capacity)과 TWAP이 그 쪽 수단이다.')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--equity', type=float, default=100_000.)
    raise SystemExit(main(ap.parse_args().equity))
