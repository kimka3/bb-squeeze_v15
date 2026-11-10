"""Run the audited backtest over a subset of symbols and save first-class cases.

The audited artifacts keep all eleven symbols so they stay reproducible. Live
trades fewer, so the number to judge live by has to come from the same engine
over the same subset. This writes cases in exactly the shape risk_sweep does
(same metrics keys), so monthly_report and the artifact pipeline read them
without special-casing.

    python src/live/universe_run.py --drop BCHUSDT --out results_live10_20261109
    python src/live/universe_run.py --drop BCHUSDT ADAUSDT LTCUSDT AVAXUSDT LINKUSDT \
        --out results_live6_20261109
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data_io                                                       # noqa: E402
import frontier_engine                                               # noqa: E402
from frontier_engine import Strategy                                 # noqa: E402
from run_frontier import ROOT, market_data, save_case, write_csv_atomic  # noqa: E402

COSTS = {'zero_fee': (0., .0002), 'binance': (.0005, .0002),
         'zero_fee_slip5': (0., .0005), 'binance_slip5': (.0005, .0005)}


def main(drop: list[str], out: Path) -> int:
    universe = [s for s in data_io.SYMBOLS if s not in drop]
    if 'BTCUSDT' not in universe:
        raise SystemExit('BTCUSDT must stay: the regime filter reads it')
    market = market_data()
    saved = frontier_engine.SYMBOLS, frontier_engine.LONGS
    frontier_engine.SYMBOLS = universe
    frontier_engine.LONGS = [s for s in data_io.LONGS if s in universe]
    print(f'{len(universe)}종목: {" ".join(s.replace("USDT", "") for s in universe)}\n')
    try:
        for base, (fee, slip) in COSTS.items():
            for cap in (4, 6):
                name = f'{base}_cap{cap}'
                cfg = Strategy(name=f'{name}_risk200', risk=.02, short_btc_bull_risk=.5,
                               fee=fee, slippage=slip, total_risk=cap / 100)
                folder = out / 'cases' / cfg.name
                r, row, need = save_case(market, cfg, folder)
                if need or row is None:
                    # A different subset holds different positions, so it can need
                    # 5m paths the eleven-symbol run never touched. Say which.
                    jobs = out / 'missing_micro.json'
                    jobs.parent.mkdir(parents=True, exist_ok=True)
                    jobs.write_text(json.dumps(sorted([s, int(t)] for s, t in need)))
                    raise SystemExit(f'{cfg.name}: 5분봉 경로 {len(need)}개 누락 -> {jobs}\n'
                                     f'python src/fetch_data.py --micro {jobs} 후 다시 실행')
                # Same enrichment as risk_sweep.execute().
                e, t = r['equity'], r['trades']
                real = 100 * t.initial_risk / t.entry_equity
                util = real / t.requested_risk_pct
                lev = e.gross / e.equity
                row.update(model=name, base_risk_pct=cfg.risk * 100,
                           bull_short_risk_pct=cfg.risk * 50,
                           clipped_entries=int((util < 1 - 1e-8).sum()),
                           average_requested_risk_pct=float(t.requested_risk_pct.mean()),
                           average_filled_risk_pct=float(real.mean()),
                           average_filled_fraction=float(util.mean()),
                           average_open_initial_risk_pct=float((100 * e.initial_risk / e.equity).mean()),
                           max_open_initial_risk_at_4h_close_pct=float((100 * e.initial_risk / e.equity).max()),
                           max_gross_at_4h_close=float(lev.max()),
                           fraction_4h_above_1x=float((lev > 1).mean()),
                           universe=universe)
                (folder / 'metrics.json').write_text(json.dumps(row, default=float, indent=2))
                write_csv_atomic(e, folder / 'equity_4h_risk.csv')
                print(f"{cfg.name:28s} CAGR {row['cagr_pct']:>6.2f}%  "
                      f"MDD(5분) {row['mdd_5m_close_pct']:>7.2f}%  "
                      f"PF {row['profit_factor']:.3f}  거래 {row['trades']:>3d}")
    finally:
        frontier_engine.SYMBOLS, frontier_engine.LONGS = saved
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--drop', nargs='*', default=['BCHUSDT'])
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    raise SystemExit(main(a.drop, ROOT / a.out))
