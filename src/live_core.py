"""Shared decision core. The backtest and the live bot run this same code.

No I/O, no clock, no randomness, no exchange calls. Everything the decision
needs is an argument. frontier_engine.run() calls these bar by bar over
history; the live runner calls them once per real 4h bar.

Position state is the same dict shape on both sides, so there is no translation
layer between backtest and live that could silently drift. That is the whole
point of this module — if you are tempted to reimplement any of this for the
live path, don't.
"""
import math
from typing import NamedTuple

from trade_core import execution_price, mark_partial_taken, remaining_initial_risk, tighten_stop

# Exits the exchange cannot evaluate for us: decided at a 4h close, executed at
# the next 4h open as a reduce-only market order.
CLOSE_RULE_EXITS = {'BB_MID', 'BB_CONFIRM2', 'BB_HALF', 'FAILED_BREAKOUT', 'NO_PROGRESS', 'TIME'}


def remaining_risk(p):
    """Initial risk still committed, scaled down by whatever has been closed."""
    return remaining_initial_risk(p['initial_risk'], p['initial_qty'], p['qty'])


class EntryPlan(NamedTuple):
    qty: float
    px: float
    unit: float
    desired: float
    skipped: str          # '' accepted | 'invalid' unusable input | 'cap' blocked by a limit


def plan_entry(symbol, side, atr, wealth, opens, positions, config, costs,
               btc_bull=False, correlated=None):
    """Size one entry against the portfolio caps.

    Caller MUST invoke this in canonical symbol order and apply each accepted
    entry before planning the next: the risk and notional room both shrink as
    positions are added, so a parallel pass would overshoot the caps and give a
    different book than the backtest.

    btc_bull   BTC closed at or above its SMA200 on the signal bar (halves new short risk)
    correlated callable(side) -> positions counted against correlated_cap_r; only
               consulted when that cap is enabled.
    """
    unit = atr * (config.long_stop_atr if side == 'LONG' else config.short_stop_atr)
    if wealth <= 0 or not math.isfinite(unit) or unit <= 0:
        return EntryPlan(0., 0., 0., 0., 'invalid')
    px = execution_price(opens[symbol], side, True, costs)
    desired = wealth * config.risk * (config.short_risk_multiple if side == 'SHORT' else 1)
    if side == 'SHORT' and config.short_btc_bull_risk != 1. and btc_bull:
        desired *= config.short_btc_bull_risk
    room = wealth * config.total_risk - sum(remaining_risk(p) for p in positions.values())
    if side == 'SHORT' and config.short_cap_r:
        room = min(room, wealth * config.risk * config.short_cap_r
                   - sum(remaining_risk(p) for p in positions.values() if p['side'] == 'SHORT'))
    if config.correlated_cap_r and positions:
        room = min(room, wealth * config.risk * config.correlated_cap_r
                   - sum(remaining_risk(p) for p in correlated(side)))
    notional_room = wealth * config.gross_cap - sum(p['qty'] * opens[a] for a, p in positions.items())
    q = min(desired / unit, max(0, room) / unit, max(0, notional_room) / px)
    if len(positions) >= config.max_positions or q < desired / unit * .1:
        return EntryPlan(0., px, unit, desired, 'cap')
    return EntryPlan(q, px, unit, desired, '')


def open_position(side, ts, signal_ms, plan, qty, wealth, fee, breakout_level):
    """Build position state from an entry that actually filled.

    qty is the FILLED quantity, which live may report below plan.qty. Risk
    accounting keys off it so a partial fill books the risk it really took.
    """
    px, unit = plan.px, plan.unit
    return {'side': side, 'entry_ms': ts, 'signal_ms': signal_ms, 'entry': px,
            'initial_qty': qty, 'qty': qty, 'risk_unit': unit, 'initial_risk': qty * unit,
            'entry_equity': wealth, 'requested_risk_pct': 100 * plan.desired / wealth,
            'breakout_level': breakout_level,
            'stop': px - unit if side == 'LONG' else px + unit, 'tp2r': px - 2 * unit,
            'partial_taken': False, 'long_half_taken': False, 'below_mid': 0, 'bars': 0,
            'lowest': px, 'highest': px, 'gross_pnl': 0., 'fees': fee, 'funding': 0.,
            'mfe': 0., 'mae': 0.,
            'fills': [{'time_ms': ts, 'role': 'ENTRY', 'qty': qty, 'price': px, 'fee': fee, 'gross_pnl': 0.}]}


def update_on_bar_close(p, row, config):
    """Trails and close-rule exits at a 4h close. Mutates p.

    Trails move only at bar closes and only in the favourable direction, which
    is why the live bot can park the stop on the exchange and amend it once per
    bar instead of watching ticks.
    """
    if p['side'] == 'LONG':
        below = float(row['close']) < float(row['l_bb_mid'])
        p['below_mid'] = p['below_mid'] + 1 if below else 0
        p['highest'] = max(p['highest'], float(row['high']))
        if config.long_exit == 'bb_mid' and below:
            p['pending_exit'] = 'BB_MID'
        elif config.long_exit == 'confirm2' and p['below_mid'] >= 2:
            p['pending_exit'] = 'BB_CONFIRM2'
        elif config.long_exit == 'half_runner' and below and not p['long_half_taken']:
            p['pending_exit'] = 'BB_HALF'
        if config.long_exit == 'atr' or (config.long_exit == 'half_runner' and p['long_half_taken']):
            tighten_stop(p, p['highest'] - config.long_trail * float(row['l_atr']))
        if p['bars'] >= 180:
            p['pending_exit'] = 'TIME'
    else:
        p['lowest'] = min(p['lowest'], float(row['low']))
        if p['entry'] - p['lowest'] >= config.trail_activation_r * p['risk_unit']:
            tighten_stop(p, p['lowest'] + config.short_trail_atr * float(row['s_atr']))
        if p['partial_taken']:
            tighten_stop(p, p['entry'])
        if config.short_reject_bars and p['bars'] <= config.short_reject_bars \
                and not p['partial_taken'] and float(row['close']) > p['breakout_level']:
            p['pending_exit'] = 'FAILED_BREAKOUT'
        if config.short_progress_bars and p['bars'] == config.short_progress_bars \
                and float(row['close']) >= p['entry']:
            p['pending_exit'] = 'NO_PROGRESS'
        if p['bars'] >= 90:
            p['pending_exit'] = 'TIME'


def scan_signals(ts, rows, symbols, longs, positions, setups, config):
    """New entry signals from a 4h close. Mutates setups (the long retest window).

    Returns (pending, skipped_filter). pending maps symbol -> (side, atr, signal_ms)
    and is consumed at the NEXT bar open.
    """
    pending = {}
    skipped = 0
    for s in symbols:
        r = rows[s]
        if s in positions:
            setups.pop(s, None)
            continue
        short = bool(r['short_signal'])
        long = False
        if short and config.short_min_atr_pct and float(r['s_atr']) / float(r['close']) < config.short_min_atr_pct:
            short = False
            skipped += 1
        if short and config.short_btc_bear:
            btc = rows['BTCUSDT']
            if float(btc['close']) >= float(btc['x_ma200']):
                short = False
                skipped += 1
        if s in longs:
            if bool(r['long_breakout']):
                setups[s] = [float(r['l_bb_upper']), config.long_retest_bars]
            elif s in setups:
                ref, left = setups[s]
                long = (float(r['low']) <= ref * 1.005 and float(r['close']) > ref
                        and float(r['close']) > float(r['open'])
                        and float(r['close']) > float(r['l_ma200'])
                        and float(r['l_ma200_slope']) > 0)
                if long or left <= 1:
                    setups.pop(s, None)
                else:
                    setups[s][1] -= 1
        if short and not long:
            pending[s] = ('SHORT', float(r['s_atr']), ts)
        if long and not short:
            pending[s] = ('LONG', float(r['l_atr']), ts)
    return pending, skipped


# The 2R partial filled. Live calls this from the exchange fill event; the
# backtest reaches the same function inside trade_core.intrabar().
apply_tp_fill = mark_partial_taken


def btc_bull_at(row):
    """BTC closed at or above SMA200 on the signal bar: new shorts take half risk."""
    return float(row['close']) >= float(row['x_ma200'])
