"""CLI. Defaults to shadow mode; live requires an explicit flag and a key.

    python src/live/run.py --once                  decide on the latest closed bar, no orders
    python src/live/run.py --loop                  keep running on the 4h boundary
    python src/live/run.py --mode paper --loop     live data, simulated fills (stage 2)
    python src/live/run.py --mode paper --loop --until 2026-10-10T12:00Z   stop on a date
    python src/live/run.py --mode paper --report   what paper mode has measured so far
    python src/live/run.py --mode live --loop      real orders (needs LIGHTER_API_PRIVATE_KEY)

Paper mode runs two clocks, like production: decisions on the 4h boundary, and a
fast poll in between that checks resting stops against the MARK price — the
trigger basis Lighter actually uses, which the backtest does not model.

--until takes an ABSOLUTE instant rather than a duration on purpose. A multi-week
run gets restarted — reboots, dropped links, a supervisor — and a duration would
restart its clock every time, so the run would never end. State survives restarts
either way: last_bar_ms makes reprocessing a bar a no-op.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_io import H4, SYMBOLS                                    # noqa: E402
from live.broker import DryRunBroker, LighterBroker                # noqa: E402
from live.paper import PaperBroker                                 # noqa: E402
from live.config import LiveConfig, SYMBOL_TO_LIGHTER              # noqa: E402
from live.journal import Journal                                   # noqa: E402
from live.markets import load_markets, marks                       # noqa: E402
from live.trader import Trader                                     # noqa: E402
from live import feed                                              # noqa: E402
from live import observation                                       # noqa: E402

STOP_REQUESTED = threading.Event()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, allow_nan=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def ensure_identity(config) -> None:
    path = config.state_dir / 'identity.json'
    description = config.describe()
    description.pop('state_dir', None)
    digest = hashlib.sha256(json.dumps(description, sort_keys=True).encode()).hexdigest()
    identity = {'mode': config.mode, 'config_sha256': digest, 'config': description}
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        if old['mode'] != config.mode or old['config_sha256'] != digest:
            raise RuntimeError('state belongs to a different mode/config; use a new campaign')
    else:
        if any((config.state_dir / name).exists() for name in ('snapshot.json', 'paper.json')):
            raise RuntimeError('legacy state has no config identity; preserve it and start a new campaign')
        atomic_json(path, identity)


def build(config: LiveConfig):
    markets = load_markets(config.base_url, config.lighter_universe)
    journal = Journal(config.state_dir)
    if config.mode == 'live':
        broker = LighterBroker(config.base_url, config.account_index,
                               config.private_key, config.api_key_index,
                               stop_limit_pct=config.max_slippage_pct,
                               passive_take_profit=config.passive_take_profit)
    elif config.mode == 'paper':
        broker = PaperBroker(config.base_url, markets,
                             config.state_dir / 'paper.json',
                             equity=config.paper_equity,
                             breakeven_on_fill=config.breakeven_on_fill,
                             passive_take_profit=config.passive_take_profit,
                             passive_fill_model=config.passive_fill_model,
                             stop_limit_pct=config.max_slippage_pct,
                             request_timeout_seconds=config.request_timeout_seconds)
        broker.refresh_marks()
    else:
        broker = DryRunBroker()
    atomic_json(config.state_dir / 'markets.json', {s: asdict(m) for s, m in markets.items()})
    return Trader(config, broker, markets, journal), markets


def poll_until(trader, deadline: float, config) -> None:
    """Fast loop between 4h boundaries: let resting stops fire on the mark.

    A real exchange triggering a stop does not guarantee a fully closed position.
    The trader service therefore runs after every virtual poll as well.
    """
    broker = trader.broker
    if not isinstance(broker, PaperBroker):
        time.sleep(max(1.0, deadline - time.time()))
        return
    while time.time() < deadline:
        try:
            for fill in broker.poll():
                print(f'   FILL  {fill.symbol:9s} {fill.role:5s} qty {fill.qty:.6f} '
                      f'@ {fill.price:.6f}  mark {fill.mark:.6f}  '
                      f'slip {fill.slippage_bps:+.2f}bp')
                trader.journal.append('paper_fill', **{k: v for k, v in
                                                       fill.__dict__.items()})
            trader.service()
        except Exception as exc:
            trader.journal.append('poll_error', error=str(exc))
        time.sleep(max(5.0, min(config.poll_seconds, deadline - time.time())))


def report(trader) -> None:
    broker = trader.broker
    if not isinstance(broker, PaperBroker):
        print('report is only meaningful in paper mode')
        return
    m = broker.measurements()
    print('SIMULATED account: public observations do not certify real exchange fills')
    print(f"equity {m['equity']:,.2f}   cash {m['cash']:,.2f}   "
          f"open {m['open_positions']}   fills {m['fills']}")
    print(f"\nslippage vs the backtest's flat {m['backtest_assumption_bps']:.1f}bp assumption")
    for key in ('slippage_entry', 'slippage_stop', 'slippage_take_profit',
                'slippage_close_rule_exit'):
        s = m[key]
        label = key.replace('slippage_', '')
        if not s:
            print(f'   {label:18s} no fills yet')
            continue
        print(f"   {label:18s} n={s['n']:<4d} mean {s['mean_bps']:+7.2f}bp   "
              f"median {s['median_bps']:+7.2f}bp   worst {s['worst_bps']:+7.2f}bp")
    g = m['trigger_to_fill_bps']
    if g['n']:
        print(f"\ntrigger -> fill gap  n={g['n']}  mean {g['mean']:.2f}bp  worst {g['worst']:.2f}bp")
        print('   simulated gap at sampled observations, not real exchange fill latency')
    if m['depth_exhausted_fills']:
        print(f"\nWARNING {m['depth_exhausted_fills']} fill(s) ran out of visible depth — "
              'the strategy wanted more size than the book showed')
    print(f"\nmodeled cumulative funding {m['funding_paid']:+,.4f}")


def depth_scan(markets, config, equity: float, notional_multiple: float = 0.86) -> None:
    """What today's book would charge for the sizes this strategy actually takes.

    The backtest charges a flat 2bp per side on every symbol. This prices the
    median backtest position (0.86x equity in notional) against real resting
    depth, per market. A snapshot, not a distribution — run it repeatedly through
    the paper phase to get one.
    """
    from live.book import fetch_depth, walk
    px = marks(config.base_url, markets)
    print(f'depth scan   equity {equity:,.0f}   position notional '
          f'{notional_multiple:.2f}x = {equity * notional_multiple:,.0f} USDT')
    print(f"\n{'symbol':10s}{'qty':>18s}{'sell vwap':>15s}{'sell':>9s}{'buy':>9s}{'depth':>8s}")
    worst = []
    for symbol, market in markets.items():
        price = px.get(symbol)
        if not price:
            continue
        qty = market.round_size(equity * notional_multiple / price)
        depth = fetch_depth(config.base_url, market.market_id)
        bids = [float(o['price']) for o in depth.get('bids') or []][:1]
        asks = [float(o['price']) for o in depth.get('asks') or []][:1]
        if not bids or not asks:
            print(f'{symbol:10s}  no visible book')
            continue
        mid = (bids[0] + asks[0]) / 2
        sell = walk(depth, qty, buying=False)
        buy = walk(depth, qty, buying=True)
        s_bps = sell.slippage_bps(mid, False)
        b_bps = buy.slippage_bps(mid, True)
        worst.append((max(s_bps, b_bps), symbol))
        flag = 'OUT' if (sell.exhausted or buy.exhausted) else 'ok'
        print(f'{symbol:10s}{qty:>18.5f}{sell.vwap:>15.6f}'
              f'{s_bps:>8.2f}{b_bps:>9.2f}{flag:>8s}')
    worst.sort(reverse=True)
    print(f"\nassumed in the backtest: 2.00bp per side, every symbol")
    over = [(b, s) for b, s in worst if b > 2.0]
    if over:
        print('worse than that assumption:')
        for bps, symbol in over:
            print(f'   {symbol:10s} {bps:6.2f}bp   {bps / 2.0:5.1f}x the assumption')
    print('\nOUT means the visible book held less than the strategy wanted — that '
          'size cannot be filled at any of these prices.')


def capacity(markets, config, multiple: float = 0.86) -> None:
    """How the strategy's own slippage grows with the account behind it.

    Weighted by each symbol's share of the backtest's traded notional, so the
    number answers the question that matters: what does THIS strategy pay, not
    what does an average trade pay. Slippage is a capacity constraint — the
    assumption the backtest was built on only holds up to a certain size.
    """
    import csv
    from live.book import fetch_depth, walk

    ROOT = Path(__file__).resolve().parents[2]
    trades = ROOT / 'results/cases/zero_fee_cap4_risk200/trades.csv'
    share, total = {}, 0.
    if trades.exists():
        with trades.open() as fh:
            for row in csv.DictReader(fh):
                n = float(row['initial_qty']) * float(row['entry_price'])
                share[row['symbol']] = share.get(row['symbol'], 0.) + n
                total += n
        share = {k: v / total for k, v in share.items()}
    else:
        share = {s: 1 / len(markets) for s in markets}

    px = marks(config.base_url, markets)
    depth = {s: fetch_depth(config.base_url, m.market_id) for s, m in markets.items()}

    print(f'capacity   position notional {multiple:.2f}x equity   '
          f'weighted by the backtest\'s traded notional')
    print(f"\n{'equity':>12s}{'weighted slippage':>20s}{'vs 2bp':>10s}")
    for equity in (5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000):
        weighted, problems = 0., []
        for symbol, market in markets.items():
            price = px.get(symbol)
            if not price:
                continue
            qty = market.round_size(equity * multiple / price)
            book = depth[symbol]
            bids, asks = book.get('bids') or [], book.get('asks') or []
            if qty <= 0 or not bids or not asks:
                problems.append(f'{symbol}:small')
                continue
            mid = (float(bids[0]['price']) + float(asks[0]['price'])) / 2
            q = walk(book, qty, buying=False)
            if q.exhausted:
                problems.append(f'{symbol}:OUT')
            weighted += share.get(symbol, 0.) * q.slippage_bps(mid, False)
        note = ('   ' + ' '.join(problems)) if problems else ''
        print(f'{equity:>12,}{weighted:>18.2f}bp{weighted / 2.0:>9.1f}x{note}')
    print('\nOne depth snapshot. Run it repeatedly across the paper phase: depth '
          'moves with time of day and volatility.')
    print('The backtest assumed 2.00bp per side on every symbol at every size.')


def prepare_frames(config):
    skew = feed.clock_skew_seconds(config.binance_base_url, config.request_timeout_seconds)
    if abs(skew) > config.max_clock_skew_seconds:
        raise RuntimeError(f'clock skew exceeds configured limit: {skew:.3f}s')
    return feed.prepare(list(config.universe), base_url=config.binance_base_url,
                        timeout=config.request_timeout_seconds, retries=config.feed_retries)


def run_once(trader, markets, config, frames=None, deadline=float('inf')):
    frames = prepare_frames(config) if frames is None else frames
    bar_ms, rows = feed.latest_closed(frames)
    current = marks(config.base_url, markets)
    if STOP_REQUESTED.is_set() or time.time() >= deadline:
        return None
    now_ms = int(time.time() * 1000)
    late = now_ms - (bar_ms + H4) > config.bar_grace_seconds * 1000
    # Late bars may restore indicators / manage existing positions, never open
    # the missed signal at an unrelated later price.
    outcome = trader.on_bar(bar_ms, rows, current, now_ms=now_ms, allow_entries=not late)
    stamp = time.strftime('%Y-%m-%d %H:%M', time.gmtime(bar_ms / 1000))
    print(f'[{config.mode}] bar {stamp}Z  equity {outcome.equity:,.2f}  '
          f'positions {len(trader.positions)}')
    for e in outcome.exits:
        print(f'   EXIT  {e["symbol"]:9s} {e["reason"]:16s} qty {e["qty"]:.6f}')
    for e in outcome.entries:
        print(f'   ENTER {e["symbol"]:9s} {e["side"]:5s} qty {e["qty"]:.6f} '
              f'@ {e["price"]:.6f}  stop {e["stop"]:.6f}')
    for symbol, why in outcome.skipped:
        print(f'   skip  {symbol:9s} {why}')
    for r in outcome.reasons:
        print(f'   note  {r}')
    if outcome.halted:
        print('   HALTED — entries stopped. Inspect protection state and persisted reason.')
    return outcome


def next_boundary(now_ms: int) -> int:
    return (now_ms // H4 + 1) * H4


def parse_until(text: str) -> float:
    """ISO 8601 instant -> unix seconds. Naive input is read as UTC, because every
    other clock in this system is UTC and a silent local-time reading would end a
    multi-week run hours early or late."""
    t = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def persist_report(trader, config, **health) -> None:
    now_ms = int(time.time() * 1000)
    if isinstance(trader.broker, PaperBroker):
        payload = trader.broker.measurements()
        payload.update({'updated_ms': now_ms, 'mode': 'paper',
                        'classification': 'simulation_not_exchange_account',
                        'reporting_currency': 'USDC virtual ledger',
                        'strategy_data_gaps': list(getattr(trader, 'data_gaps', [])),
                        'trader_halted': bool(getattr(trader, 'halted', False)),
                        'halt_reasons': list(getattr(trader, 'halt_reasons', [])),
                        'config': config.describe()})
        if payload['strategy_data_gaps']:
            payload['performance_complete'] = False
        atomic_json(config.state_dir / 'report.json', payload)
    atomic_json(config.state_dir / 'runner_status.json', {
        'updated_ms': now_ms, 'pid': os.getpid(), 'mode': config.mode,
        'last_bar_ms': trader.last_bar_ms,
        'halted': bool(getattr(trader, 'halt_reason', None) or getattr(trader, 'halted', False)),
        'halt_reasons': list(getattr(trader, 'halt_reasons', [])),
        'pending_operation': getattr(trader, 'pending_operation', None),
        'residual_exits': getattr(trader, 'residual_exits', {}),
        'data_gaps': list(getattr(trader, 'data_gaps', [])),
        **health})


def run_loop(trader, markets, config, deadline: float) -> None:
    """Single account writer; background tasks are strictly read-only market I/O.

    Slow Binance warmup never blocks the paper protection loop. An observation
    task only reads public data; it cannot place or mutate account orders.
    """
    pool = ThreadPoolExecutor(max_workers=2)
    bar_future = sample_future = None
    next_bar_at = next_poll_at = next_sample_at = time.time()
    counters = {'poll_errors': 0, 'bar_errors': 0, 'observation_errors': 0,
                'successful_polls': 0, 'observations': 0, 'last_error': None,
                'last_successful_poll_ms': None, 'last_bar_success_ms': None,
                'last_observation_ms': None}
    previous = config.state_dir / 'runner_status.json'
    if previous.exists():
        old = json.loads(previous.read_text(encoding='utf-8'))
        counters.update({k: old[k] for k in counters if k in old})
    counters['session_started_ms'] = int(time.time() * 1000)
    counters['counter_scope'] = 'cumulative_for_state_directory'
    try:
        while time.time() < deadline and not STOP_REQUESTED.is_set():
            now = time.time()
            if bar_future is None and now >= next_bar_at:
                bar_future = pool.submit(prepare_frames, config)
            if bar_future is not None and bar_future.done():
                try:
                    outcome = run_once(trader, markets, config, bar_future.result(), deadline=deadline)
                    if outcome is not None:
                        counters['last_bar_success_ms'] = int(time.time() * 1000)
                    next_bar_at = next_boundary(int(time.time() * 1000)) / 1000 + min(5, config.bar_grace_seconds / 4)
                except Exception as exc:
                    counters['bar_errors'] += 1
                    counters['last_error'] = f'bar: {exc}'
                    trader.journal.append('bar_error', error=str(exc))
                    next_bar_at = time.time() + config.poll_seconds
                bar_future = None
            now = time.time()
            if now >= deadline or STOP_REQUESTED.is_set():
                break
            if now >= next_poll_at:
                poll_failed = False
                try:
                    if isinstance(trader.broker, PaperBroker):
                        for fill in trader.broker.poll():
                            trader.journal.append('paper_fill', **fill.__dict__)
                            print(f'SIMULATED FILL {fill.symbol} {fill.role} qty={fill.qty} price={fill.price}', flush=True)
                    counters['successful_polls'] += 1
                    counters['last_successful_poll_ms'] = int(time.time() * 1000)
                except Exception as exc:
                    poll_failed = True
                    counters['poll_errors'] += 1
                    counters['last_error'] = f'poll: {exc}'
                    trader.journal.append('poll_error', error=str(exc))
                # Earlier fills can be persisted before a later poll request
                # fails. Reconcile them without stale-price close retries.
                try:
                    trader.service(marks={} if poll_failed else None)
                except Exception as exc:
                    counters['last_error'] = f'service: {exc}'
                    trader.journal.append('service_error', error=str(exc))
                next_poll_at = time.time() + config.poll_seconds
                persist_report(trader, config, **counters)
            now = time.time()
            if now >= deadline or STOP_REQUESTED.is_set():
                break
            if sample_future is None and now >= next_sample_at and isinstance(trader.broker, PaperBroker):
                sample_future = pool.submit(observation.collect, config, markets, trader.broker.equity())
            if sample_future is not None and sample_future.done():
                try:
                    observation.save(config.state_dir, sample_future.result())
                    counters['observations'] += 1
                    counters['last_observation_ms'] = int(time.time() * 1000)
                except Exception as exc:
                    counters['observation_errors'] += 1
                    counters['last_error'] = f'observation: {exc}'
                    trader.journal.append('observation_error', error=str(exc))
                sample_future = None
                next_sample_at = time.time() + config.observation_seconds
            STOP_REQUESTED.wait(max(0, min(1, deadline - time.time())))
    finally:
        # The account has one writer. Checkpoint before waiting on read-only
        # workers, so a supervisor timeout cannot discard the final ledger.
        if isinstance(trader.broker, PaperBroker):
            trader.broker.save()
        persist_report(trader, config, **counters)
        pool.shutdown(wait=True, cancel_futures=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--config')
    ap.add_argument('--mode', choices=('shadow', 'paper', 'live'))
    ap.add_argument('--state-dir')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--status', action='store_true', help='read last persisted runner health offline')
    ap.add_argument('--preflight', action='store_true', help='public-data readiness check, no orders')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--until', help='stop the loop at this UTC instant '
                                    '(ISO 8601, e.g. 2026-10-10T12:00Z)')
    ap.add_argument('--depth-scan', action='store_true', dest='depth_scan')
    ap.add_argument('--capacity', action='store_true')
    ap.add_argument('--equity', type=float, default=100_000.)
    ap.add_argument('--i-understand-this-places-real-orders', action='store_true',
                    dest='confirmed')
    a = ap.parse_args()

    overrides = {}
    if a.mode:
        overrides['mode'] = a.mode
    if a.state_dir:
        overrides['state_dir'] = a.state_dir
    config = LiveConfig.load(a.config, **overrides)

    if a.report or a.status:
        path = config.state_dir / ('runner_status.json' if a.status else 'report.json')
        if not path.exists():
            print(json.dumps({'available': False, 'path': str(path), 'reason': 'no persisted run result'}))
            return 1
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['read_at_ms'] = int(time.time() * 1000)
        saved['snapshot_age_seconds'] = max(0, (saved['read_at_ms'] - saved.get('updated_ms', 0)) / 1000)
        print(json.dumps(saved, indent=2, ensure_ascii=False))
        return 0

    if a.preflight:
        from live.preflight import check
        checked = check(config)
        print(json.dumps(checked, indent=2, ensure_ascii=False, default=str))
        return 0 if checked['ready'] else 1

    if config.mode == 'live' and not a.confirmed:
        print('Refusing to start: mode is live but --i-understand-this-places-real-orders '
              'was not passed.', file=sys.stderr)
        return 2
    if config.mode == 'live':
        print('Live execution is disabled: terminal exchange fill/protection confirmation '
              'has not been integrated and verified. Use --mode paper.', file=sys.stderr)
        return 2

    if a.depth_scan or a.capacity:
        loaded = load_markets(config.base_url, config.lighter_universe)
        if a.depth_scan:
            depth_scan(loaded, config, a.equity)
        if a.capacity:
            capacity(loaded, config)
        return 0

    deadline = parse_until(a.until) if a.until else float('inf')
    if deadline <= time.time():
        print('Deadline already reached; no broker or feed started.')
        return 0
    from live.process_lock import ProcessLock
    with ProcessLock(config.state_dir / 'runner.lock'):
        STOP_REQUESTED.clear()
        signal.signal(signal.SIGTERM, lambda *_: STOP_REQUESTED.set())
        signal.signal(signal.SIGINT, lambda *_: STOP_REQUESTED.set())
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, lambda *_: STOP_REQUESTED.set())
        ensure_identity(config)
        trader, markets = build(config)
        trader.journal.append('start', config=config.describe())
        if a.once or not a.loop:
            run_once(trader, markets, config)
            persist_report(trader, config)
            return 0
        print(f'Running {config.mode} until {a.until or "stopped"}; all paper fills are simulated.', flush=True)
        run_loop(trader, markets, config, deadline)
        reason = 'stop requested' if STOP_REQUESTED.is_set() else 'until reached'
        trader.journal.append('stop', reason=reason)
        print(f'{reason}; elapsed time alone is not qualification.', flush=True)
        report(trader)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
