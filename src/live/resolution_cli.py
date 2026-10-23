"""Operator evidence review and request queue; no exchange connection or orders.

inspect -> prepare explicit IDs with rationale -> inspect saved case -> submit.
The live runner alone can apply a case after fresh reconciliation. A new case
is required if the reviewed state changes or the five-minute window expires.
"""
import argparse
import json
import os

from .production import load, activation
from .resolution_policy import reason_id, classify_reason
from .safety_resolution import ResolutionStore, read_json, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('inspect')
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--action', choices=('release-halt', 'reparse-history'), required=True)
    prepare.add_argument('--reason-id', action='append', default=[])
    prepare.add_argument('--record', action='append', default=[])
    prepare.add_argument('--reason', required=True, help='operator review rationale; never enter credentials')
    for name in ('show', 'submit', 'status'):
        sub = commands.add_parser(name); sub.add_argument('case_id')
    args = parser.parse_args()
    if os.name != 'nt' and os.geteuid() != 0:
        parser.error('use the root-owned production operator wrapper')
    cfg, live, fingerprint = load(args.config)
    approved = activation(live.state_dir/'activation.json', fingerprint, live.account_index)
    store = ResolutionStore(live.state_dir, fingerprint, live.account_index,
                            cfg['authentication']['network'], approved['started_at_ms'])
    if args.command == 'inspect':
        evidence = store.inspect(); snapshot = evidence['snapshot']
        result = {'identity':store.identity, 'halted':snapshot['halted'],
                  'halt_reasons':[{'reason_id':reason_id(r), 'reason':r,
                      'release_category':classify_reason(r, live.universe)} for r in snapshot['halt_reasons']],
                  'positions':snapshot['positions'], 'pending_operation':snapshot['pending_operation'],
                  'unresolved_operations':snapshot['unresolved_operations'],
                  'history_candidates':evidence['history_candidates'],
                  'evidence_sha256':digest(evidence),
                  'entry_paused':(live.state_dir/'ENTRY_PAUSED').exists(),
                  'exchange_paused':(live.state_dir/'EXCHANGE_PAUSED').exists(),
                  'orders_submitted':0,
                  'meaning':'inspection only; missing orders are not evidence of no submission'}
    elif args.command == 'prepare':
        if (args.action == 'release-halt' and args.record
                or args.action == 'reparse-history' and args.reason_id):
            parser.error('select only IDs corresponding to the explicit action')
        case = store.prepare(args.action, args.reason_id or args.record, args.reason, live.universe)
        result = {k:v for k,v in case.items() if k != 'evidence'}
        result['case_path'] = str(store.path('cases', case['case_id']))
        result['next_step'] = 'show the case evidence, then submit this exact case ID'
    elif args.command == 'submit':
        result = store.submit(args.case_id)
    elif args.command == 'show':
        result = read_json(store.path('cases', args.case_id))
    else:
        case = read_json(store.path('cases', args.case_id))
        result_path = store.path('outcomes', args.case_id)
        result = read_json(result_path) if result_path.exists() else {
            'case_id':args.case_id,
            'state':'queued' if store.path('requests', args.case_id).exists() else 'prepared',
            'expires_ms':case['expires_ms'],
            'meaning':'no outcome yet; not proof of release or permission to trade'}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
