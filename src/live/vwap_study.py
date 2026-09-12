"""Would slicing the order (Lighter TWAP) beat taking it in one sweep?

Lighter's TWAP places a market order every 30 seconds, sized total/((duration/30)+1).
Every slice is a TAKER order, so it saves no spread. The only thing it can win is
impact: if the book refills between slices, each slice walks a shallower distance
than one sweep would.

That "if" is the whole question, and it is measurable rather than assumable.
Lighter returns individual resting orders with a stable order_index, so we can
see exactly how much of the book at t+30s was not there at t.

Two halves, measured separately so they do not get confused:

  BENEFIT  impact reduction, simulated with the order book actually being
           CONSUMED by each slice and restored only at the measured refill rate.
           Comparing independent snapshots instead — walking the same resting
           orders again and again — invents a saving that is not there.
  COST     timing drift, from the 427 real entry bars at 5m resolution.

    python src/live/vwap_study.py --sample 11 --gap 30    # collect and analyse
    python src/live/vwap_study.py --snapshots /tmp/vwap/snaps.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np                                                  # noqa: E402
import pandas as pd                                                 # noqa: E402

from live.book import fetch_depth, walk                             # noqa: E402
from live.config import MAINNET, SYMBOL_TO_LIGHTER                  # noqa: E402
from live.markets import load_markets, marks                        # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TRADES = ROOT / 'results/cases/zero_fee_cap4_risk200/trades.csv'


def collect(count: int, gap: int, path: Path) -> list[dict]:
    markets = load_markets(MAINNET, SYMBOL_TO_LIGHTER)
    snaps = []
    for i in range(count):
        started = time.time()
        snap = {'t': started, 'books': {}}
        for symbol, market in markets.items():
            try:
                snap['books'][symbol] = fetch_depth(MAINNET, market.market_id)
            except Exception:
                pass
        snaps.append(snap)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snaps))
        print(f'  sample {i + 1}/{count}  {len(snap["books"])} books', flush=True)
        if i < count - 1:
            time.sleep(max(0, gap - (time.time() - started)))
    return snaps


def _mid(book: dict) -> float | None:
    bids, asks = book.get('bids') or [], book.get('asks') or []
    if not bids or not asks:
        return None
    return (float(bids[0]['price']) + float(asks[0]['price'])) / 2


def _levels(book: dict) -> list[list]:
    rows = []
    for o in book.get('bids') or []:
        try:
            price, size = float(o['price']), float(o.get('remaining_base_amount') or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            rows.append([price, size])
    rows.sort(key=lambda x: -x[0])
    return rows


def turnover(snaps: list[dict], symbol: str) -> float:
    """Fraction of resting bid notional replaced by NEW orders per interval.

    Measured from order_index, not assumed. This is the ceiling on what slicing
    can recover: depth that never comes back cannot be walked twice.
    """
    shares = []
    for a, b in zip(snaps, snaps[1:]):
        ba, bb = a['books'].get(symbol), b['books'].get(symbol)
        if not ba or not bb:
            continue
        before = {o['order_index'] for o in ba.get('bids') or []}
        after = {o['order_index']: float(o['price']) * float(o['remaining_base_amount'])
                 for o in bb.get('bids') or []}
        total = sum(after.values())
        if total > 0:
            shares.append(sum(v for k, v in after.items() if k not in before) / total)
    return float(np.mean(shares)) if shares else 0.


def sliced_vwap(book: dict, qty: float, slices: int, refill: float) -> float | None:
    """VWAP of slicing `qty`, with the book consumed and `refill` restored between.

    refill=1 is the naive view in which every slice sees a pristine book — that
    is what comparing independent snapshots accidentally measures. refill=0 means
    slicing walks exactly the same depth as one sweep and saves nothing at all.
    """
    levels = _levels(book)
    if not levels or qty <= 0 or slices < 1:
        return None
    start = [list(r) for r in levels]
    take = qty / slices
    cost = filled = 0.
    for _ in range(slices):
        want = take
        for row in levels:
            if want <= 1e-15:
                break
            got = min(want, row[1])
            cost += got * row[0]
            filled += got
            row[1] -= got
            want -= got
        if want > 1e-12:
            return None                       # the book ran out entirely
        for row, original in zip(levels, start):
            row[1] = min(original[1], row[1] + original[1] * refill)
    return cost / filled if filled else None


def impact_reduction(snaps, markets, prices, equity: float,
                     multiple: float = 0.86) -> list[dict]:
    rows = []
    n = len(snaps)
    for symbol, market in markets.items():
        price, first = prices.get(symbol), snaps[0]['books'].get(symbol)
        if not price or not first:
            continue
        full = market.round_size(equity * multiple / price)
        base = _mid(first)
        if not base or full <= 0:
            continue
        one = walk(first, full, buying=False)
        if one.filled <= 0:
            continue
        r = turnover(snaps, symbol)
        real = sliced_vwap(first, full, n, r)
        ideal = sliced_vwap(first, full, n, 1.0)
        row = {'symbol': symbol, 'refill': r, 'slices': n,
               'sweep_bps': one.slippage_bps(base, False),
               'sliced_bps': (base - real) / base * 1e4 if real else None,
               'ideal_bps': (base - ideal) / base * 1e4 if ideal else None}
        row['saved'] = (row['sweep_bps'] - row['sliced_bps']) if row['sliced_bps'] is not None else None
        row['ideal_saved'] = (row['sweep_bps'] - row['ideal_bps']) if row['ideal_bps'] is not None else None
        rows.append(row)
    return rows


def timing_drift(minutes=(5, 10, 15, 30)) -> pd.DataFrame:
    """What waiting costs, from the real entry bars. Positive = waiting was worse."""
    from run_frontier import market_data
    market = market_data()
    rows = []
    for tr in pd.read_csv(TRADES).to_dict('records'):
        micro = market.micro.get((tr['symbol'], int(tr['entry_ms'])))
        if micro is None:
            continue
        open_px = float(micro[0][1])
        rec = {'symbol': tr['symbol'], 'side': tr['side']}
        for m in minutes:
            k = m // 5
            if k < len(micro):
                later = float(micro[k][4])
                worse = (open_px - later) if tr['side'] == 'SHORT' else (later - open_px)
                rec[m] = worse / open_px * 1e4
        rows.append(rec)
    return pd.DataFrame(rows)


def main(args) -> int:
    path = Path(args.snapshots)
    if args.sample:
        print(f'collecting {args.sample} snapshots, {args.gap}s apart '
              f'(~{args.sample * args.gap / 60:.1f} min)')
        snaps = collect(args.sample, args.gap, path)
    else:
        snaps = json.loads(path.read_text())
    if len(snaps) < 2:
        print('need at least two snapshots')
        return 1

    markets = load_markets(MAINNET, SYMBOL_TO_LIGHTER)
    prices = marks(MAINNET, markets)
    span = snaps[-1]['t'] - snaps[0]['t']
    gap = span / max(1, len(snaps) - 1)

    print('=' * 74)
    print(f'1. 분할 체결의 이득 — 호가가 되살아나는가  '
          f'({len(snaps)}슬라이스, 간격 {gap:.0f}초)')
    print('=' * 74)
    print('슬라이스마다 호가를 실제로 소진시키고, 측정된 재충전율만큼만 되살렸다.')
    print(f"\n{'symbol':10s}{'재충전/슬라이스':>16s}{'한번에':>10s}{'분할(실측)':>13s}"
          f"{'절감':>9s}{'완전재충전 가정':>17s}")
    rows = impact_reduction(snaps, markets, prices, args.equity)
    for r in sorted(rows, key=lambda x: -(x['saved'] or 0)):
        sliced = f"{r['sliced_bps']:.2f}" if r['sliced_bps'] is not None else 'n/a'
        saved = f"{r['saved']:+.2f}" if r['saved'] is not None else 'n/a'
        ideal = f"{r['ideal_saved']:+.2f}" if r['ideal_saved'] is not None else 'n/a'
        print(f"{r['symbol']:10s}{r['refill'] * 100:>15.1f}%{r['sweep_bps']:>10.2f}"
              f"{sliced:>13s}{saved:>9s}{ideal:>17s}")
    real = [r['saved'] for r in rows if r['saved'] is not None]
    ideal_all = [r['ideal_saved'] for r in rows if r['ideal_saved'] is not None]
    if real:
        print(f'\n실측 재충전 기준 평균 절감  {np.mean(real):+.2f}bp')
        print(f'완전 재충전을 가정하면      {np.mean(ideal_all):+.2f}bp   '
              '<- 스냅샷을 독립 비교하면 나오는 값. 허상이다.')

    print('\n' + '=' * 74)
    print('2. 분할 체결의 비용 — 체결 지연 (실제 진입 427건, 5분봉)')
    print('=' * 74)
    print('숏이 늦게 팔면 더 낮게 팔린다. 양수면 기다린 쪽이 불리했다는 뜻이다.')
    drift = timing_drift()
    print(f"\n{'대기':>8s}{'평균':>12s}{'중앙값':>12s}{'불리한 비율':>14s}")
    for m in (5, 10, 15, 30):
        if m in drift.columns:
            col = drift[m].dropna()
            print(f'{m:>6d}분{col.mean():>+12.2f}bp{col.median():>+11.2f}bp'
                  f'{(col > 0).mean() * 100:>13.0f}%')

    print('\n' + '=' * 74)
    print('결론')
    print('=' * 74)
    if real and 5 in drift.columns:
        saved = float(np.mean(real))
        window = min((5, 10, 15, 30), key=lambda m: abs(m - (span / 60) / 2))
        cost = float(drift[window].dropna().mean())
        print(f'재충전 절감 (실측)    {saved:+.2f}bp')
        print(f'지연 비용 (~{window}분)     {cost:+.2f}bp')
        print(f'순                   {saved - cost:+.2f}bp   '
              f'{"분할이 유리" if saved - cost > 0.5 else "의미 있는 이득 없음"}')
        worst = max(rows, key=lambda r: r['sweep_bps'])
        print(f"\n가장 비싼 {worst['symbol']}: 재충전 {worst['refill'] * 100:.1f}%/슬라이스, "
              f"절감 {worst['saved']:+.2f}bp" if worst['saved'] is not None else '')
        print('아낄 여지가 가장 큰 얇은 종목일수록 호가가 되살아나지 않는다.')
        print('분할이 통하려면 재충전이 있어야 하는데, 재충전이 있는 종목은 애초에 쌌다.')
    print('\n한계: 자기 주문이 남기는 가격 충격(impact persistence)은 포함하지 않았다.')
    print('실제 TWAP은 이 계산보다 불리하다. 스냅샷은 한 세트이고 시간대·변동성에')
    print('따라 재충전율이 달라진다. 여러 번 돌려 분포로 판단할 것.')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', type=int, default=0)
    ap.add_argument('--gap', type=int, default=30)
    ap.add_argument('--snapshots', default='/tmp/vwap/snaps.json')
    ap.add_argument('--equity', type=float, default=100_000.)
    raise SystemExit(main(ap.parse_args()))
