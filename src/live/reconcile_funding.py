"""Create a separate funding correction from a flat paper report; never trade.

The source ledger, campaign deadline, and historical MDD are never rewritten.
Run as: python -m live.reconcile_funding --report report.json
        --market XRPUSDT=7 --output funding-correction.json
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.request

from .funding import funding_path, parse_funding, funding_amount


def reconcile(report: dict, market_ids: dict[str, int], get) -> dict:
    if report.get('mode') != 'paper' or report.get('classification') != 'simulation_not_exchange_account':
        raise ValueError('only a paper report is accepted')
    if report.get('open_positions') != 0 or report.get('reconciliation'):
        raise ValueError('requires an original flat report, not an existing correction')
    initial, cash = float(report['initial_cash']), float(report['cash'])
    if not math.isfinite(initial) or initial <= 0 or not math.isfinite(cash):
        raise ValueError('invalid source cash')
    events, seen, adjustment, resolved = [], set(), 0., 0
    source_events = report['funding_events']
    if not isinstance(source_events, list):
        raise ValueError('funding_events must be a list')
    for original in source_events:
        event = copy.deepcopy(original)
        key = (event['symbol'], event['settlement_hour'])
        if key in seen:
            raise ValueError('duplicate source settlement')
        seen.add(key)
        if event['status'] == 'booked_estimate':
            events.append(event)
            continue
        # An unresolved row with a cash flow is ambiguous: do not double count.
        if event.get('amount') is not None:
            raise ValueError('unresolved source event already has an amount')
        try:
            # A rate is not enough if the original boundary index was unobserved.
            funding_amount(event, 0.)
            path = funding_path(market_ids[event['symbol']], event['settlement_hour'])
            raw = get(path)
            sample = {'request_path': path, 'response': raw}
            event['reconciliation_sample'] = sample
            sample.update(parse_funding(raw, event['settlement_hour']))
            if sample['status'] != 'resolved_rate':
                event['status'] = sample['status']
            else:
                amount = funding_amount(event, sample['fraction'])
                adjustment += amount
                resolved += 1
                event.update(status='booked_estimate', amount=amount,
                             rate=sample['signed_rate_percent'], next_retry=0)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            event.update(status='reconciliation_error', reconciliation_error=str(exc))
        events.append(event)
    unresolved = [e for e in events if e['status'] != 'booked_estimate']
    corrected = cash + adjustment
    return {
        'reconciliation': 'separate_post_campaign_funding_estimate_v1',
        'mode': 'paper', 'classification': 'simulation_not_exchange_account',
        'source_modified': False, 'initial_cash': initial, 'original_cash': cash,
        'funding_adjustment': adjustment, 'cash': corrected, 'equity': corrected,
        'profit': corrected - initial, 'return_pct': (corrected / initial - 1) * 100,
        'newly_resolved': resolved, 'funding_unresolved_count': len(unresolved),
        'funding_events': events, 'funding_unresolved': unresolved,
        'funding_ledger_complete': not unresolved and not report.get('data_quality', {}).get('legacy_state_unverified', False),
        # Resolving costs does not reconstruct missing observations or an equity path.
        'performance_complete': False,
        'observation_coverage_complete': report.get('observation_coverage_complete', False),
        'corrected_mdd_pct': None,
        'original_mdd_before_funding_pct': report.get('risk', {}).get('sampled_mdd_pct'),
        'limitations': ['near-boundary index estimate, not an exchange settlement statement',
                        'corrected historical equity path and MDD not reconstructed',
                        'original observation gaps remain; no actual exchange fills'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--market', action='append', required=True, help='strategy symbol=verified Lighter market id')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.report.resolve() or args.output.exists():
        parser.error('output must be a new path, different from the original report')
    markets = dict((s, int(i)) for s, i in (x.split('=', 1) for x in args.market))
    source = args.report.read_bytes()
    report = json.loads(source.decode('utf-8-sig'))

    def get(path):
        # Public-only endpoint; there is no account, auth or signing capability.
        with urllib.request.urlopen('https://mainnet.zklighter.elliot.ai' + path, timeout=15) as response:
            return json.load(response)

    result = reconcile(report, markets, get)
    result.update(source_sha256=hashlib.sha256(source).hexdigest(),
                  observed_at_ms=int(time.time() * 1000), market_ids=markets,
                  source_report_updated_ms=report.get('updated_ms'))
    if args.report.read_bytes() != source:
        raise RuntimeError('source changed during reconciliation')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({k: result[k] for k in ('funding_adjustment', 'profit', 'return_pct',
        'newly_resolved', 'funding_unresolved_count', 'performance_complete')}))


if __name__ == '__main__':
    main()
