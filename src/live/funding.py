"""Lighter hourly funding contract, verified against its public API and UI.

See docs/funding-repair.md for primary evidence. Rates are percent magnitudes;
direction is the paying side. Unknown or conflicting rows fail closed.
"""
from __future__ import annotations

import math


def funding_path(market_id: int, settlement_hour: int) -> str:
    boundary = settlement_hour * 3600
    # end_timestamp is exclusive in the observed API. Include the exact target,
    # not the next hour, and do not let count_back select an adjacent row.
    return (f'/api/v1/fundings?market_id={market_id}&resolution=1h'
            f'&start_timestamp={boundary - 3600}&end_timestamp={boundary + 1}&count_back=0')


def parse_funding(data: dict, settlement_hour: int) -> dict:
    if not isinstance(data, dict) or data.get('code', 200) != 200:
        raise ValueError('funding API returned unsuccessful response')
    rows = data.get('fundings')
    if not isinstance(rows, list):
        raise ValueError('fundings must be a list')
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('funding row must be an object')
        ts = float(row.get('timestamp', row.get('time', float('nan'))))
        if not math.isfinite(ts):
            raise ValueError('invalid funding timestamp')
        if ts > 1e12:
            ts /= 1000
        if ts == settlement_hour * 3600:
            matches.append(row)
    if not matches:
        return {'status': 'missing_rate', 'reason': 'exact settlement timestamp absent',
                'returned_timestamps': [r.get('timestamp', r.get('time')) for r in rows]}
    if len(matches) != 1:
        raise ValueError('duplicate funding settlement timestamp')
    row = matches[0]
    magnitude = float(row.get('rate', float('nan')))
    direction = row.get('direction')
    if not math.isfinite(magnitude) or magnitude < 0:
        raise ValueError('funding rate must be a finite nonnegative percent magnitude')
    if direction not in ('long', 'short'):
        raise ValueError('unknown funding paying direction')
    signed_percent = magnitude * (1 if direction == 'long' else -1)
    return {'status': 'resolved_rate', 'raw': dict(row),
            'signed_rate_percent': signed_percent, 'fraction': signed_percent / 100,
            'rate_unit': 'percent_per_hour', 'direction_meaning': 'paying_side'}


def funding_amount(event: dict, fraction: float) -> float:
    qty, price = float(event['qty']), float(event['reference_price'])
    if (event['side'] not in ('LONG', 'SHORT') or not math.isfinite(qty) or qty <= 0
            or not math.isfinite(price) or price <= 0 or not math.isfinite(fraction)):
        raise ValueError('invalid funding exposure or reference index')
    amount = -qty * price * fraction * (1 if event['side'] == 'LONG' else -1)
    if not math.isfinite(amount):
        raise ValueError('nonfinite funding cash flow')
    return amount
