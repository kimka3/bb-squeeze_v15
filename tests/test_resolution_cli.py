"""Operator CLI uses saved local evidence and queues; it never trades."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from data_io import START
from live import production, resolution_cli as cli, run
from live.resolution_policy import reason_id
from live.safety_resolution import ResolutionStore
from test_strategy_integration import setup


FP = 'a' * 64
REASON = 'pending order outcome unknown; manual reconciliation required'


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    venue, broker, trader = setup(tmp_path)
    trader.config.mode = 'live'
    trader.config.account_index = 100
    trader.halted = True
    trader.halt_reasons = [REASON]
    trader._save()
    broker.account_state()
    run.persist_report(trader, trader.config)
    (tmp_path / 'activation.json').write_text(json.dumps({
        'fingerprint': FP, 'account_index': 100, 'approved': True, 'started_at_ms': START}),
        encoding='utf-8')
    cfg = {'authentication': {'network': 'testnet', 'key_file': '/never/read/API_PRIVATE_SECRET',
                              'ignored_secret_for_output_test': 'DO_NOT_OUTPUT_AUTH_CONFIG'}}
    monkeypatch.setattr(cli, 'load', lambda path: (cfg, trader.config, FP))
    monkeypatch.setattr(cli, 'os', SimpleNamespace(name='posix', geteuid=lambda: 0))
    def forbidden(*args, **kwargs):
        pytest.fail('operator CLI must not access exchange, credentials, subprocess or live writer')
    monkeypatch.setattr(production, 'connect', forbidden)
    monkeypatch.setattr(production.LighterTransport, '__init__', forbidden)
    monkeypatch.setattr(production.subprocess, 'run', forbidden)
    for name in ('poll', 'account_state', 'market_order', 'cancel', 'modify_stop',
                 'place_stop', 'place_take_profit'):
        monkeypatch.setattr(broker, name, forbidden)
    try:
        yield tmp_path, trader, venue
    finally:
        broker.close()


def command(monkeypatch, capsys, *arguments):
    monkeypatch.setattr(sys, 'argv', ['resolution_cli', '--config', 'reviewed-production.json', *arguments])
    cli.main()
    output = capsys.readouterr().out
    assert 'DO_NOT_OUTPUT_AUTH_CONFIG' not in output
    assert 'API_PRIVATE_SECRET' not in output
    return json.loads(output)


def material(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()
            and path.suffix != '.lock'  # Windows locks prohibit even read access.
            and 'safety-resolution' not in path.relative_to(directory).parts}


def test_inspect_is_readonly_and_uses_saved_identity_not_authentication_secrets(campaign, monkeypatch, capsys):
    path, trader, venue = campaign
    before = material(path)
    result = command(monkeypatch, capsys, 'inspect')
    assert result['identity'] == {'fingerprint': FP, 'account': 100, 'network': 'testnet', 'start_ms': START}
    assert result['halt_reasons'] == [{'reason_id': reason_id(REASON), 'reason': REASON,
                                       'release_category': 'order_outcome'}]
    assert result['orders_submitted'] == 0
    assert material(path) == before and not (path / 'safety-resolution').exists()
    assert trader.halted and venue.sent == []


def test_prepare_show_submit_status_only_create_case_and_request(campaign, monkeypatch, capsys):
    path, trader, venue = campaign
    before = material(path)
    case = command(monkeypatch, capsys, 'prepare', '--action', 'release-halt',
                   '--reason-id', reason_id(REASON), '--reason', 'reviewed terminal order evidence')
    case_id = case['case_id']
    assert case['action'] == 'release-halt' and 'evidence' not in case
    assert not (path / 'safety-resolution' / 'requests' / (case_id + '.json')).exists()
    shown = command(monkeypatch, capsys, 'show', case_id)
    assert shown['case_id'] == case_id and shown['evidence']['snapshot']['halted']
    prepared = command(monkeypatch, capsys, 'status', case_id)
    assert prepared['state'] == 'prepared'
    submitted = command(monkeypatch, capsys, 'submit', case_id)
    assert submitted['state'] == 'queued' and 'no HALT released' in submitted['meaning']
    assert command(monkeypatch, capsys, 'status', case_id)['state'] == 'queued'
    assert material(path) == before and trader.halted and trader.halt_reasons == [REASON]
    assert venue.sent == []


def test_status_reports_recorded_outcome_without_claiming_entry_permission(campaign, monkeypatch, capsys):
    path, trader, venue = campaign
    case = command(monkeypatch, capsys, 'prepare', '--action', 'release-halt',
                   '--reason-id', reason_id(REASON), '--reason', 'reviewed')
    result = {'case_id': case['case_id'], 'state': 'rejected', 'reason': 'UNRESOLVED_EXCHANGE_ORDERS'}
    store = ResolutionStore(path, FP, 100, 'testnet', START)
    store.path('outcomes', case['case_id']).write_text(json.dumps(result), encoding='utf-8')
    assert command(monkeypatch, capsys, 'status', case['case_id']) == result
    assert trader.halted and venue.sent == []


@pytest.mark.parametrize('arguments', [[], ['prepare'], ['prepare', '--reason', 'reviewed'],
    ['prepare', '--action', 'release-halt'], ['submit'], ['show'], ['status'],
    ['resume'], ['release-halt'], ['prepare', '--action', 'ignore-history', '--reason', 'reviewed']])
def test_no_default_action_or_implicit_release(arguments, campaign, monkeypatch, capsys):
    path, trader, venue = campaign
    before = material(path)
    with pytest.raises(SystemExit) as error:
        command(monkeypatch, capsys, *arguments)
    assert error.value.code == 2
    assert material(path) == before and trader.halted and venue.sent == []
    assert not (path / 'safety-resolution').exists()


@pytest.mark.parametrize('arguments', [
    ['prepare', '--action', 'release-halt', '--reason', 'reviewed'],
    ['prepare', '--action', 'release-halt', '--reason', 'reviewed', '--reason-id', 'not-a-reason'],
    ['prepare', '--action', 'release-halt', '--reason', 'reviewed', '--reason-id', reason_id(REASON),
     '--reason-id', reason_id(REASON)],
    ['prepare', '--action', 'reparse-history', '--reason', 'reviewed', '--record', 'trade:1'],
])
def test_missing_unknown_duplicate_or_unsupported_selection_does_not_create_case(
        arguments, campaign, monkeypatch, capsys):
    path, trader, venue = campaign
    before = material(path)
    with pytest.raises(ValueError): command(monkeypatch, capsys, *arguments)
    assert not (path / 'safety-resolution').exists()
    assert material(path) == before and trader.halted and venue.sent == []


@pytest.mark.parametrize('action,wrong_flag', [('release-halt', '--record'),
                                           ('reparse-history', '--reason-id')])
def test_mixed_action_selection_is_explicitly_rejected(action, wrong_flag, campaign, monkeypatch, capsys):
    with pytest.raises(SystemExit) as error:
        command(monkeypatch, capsys, 'prepare', '--action', action, '--reason', 'reviewed', wrong_flag, '1')
    assert error.value.code == 2


@pytest.mark.parametrize('uid', [1, 1000])
def test_nonroot_linux_user_rejected_before_configuration_or_evidence_access(uid, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'os', SimpleNamespace(name='posix', geteuid=lambda: uid))
    monkeypatch.setattr(cli, 'load', lambda *a: pytest.fail('nonowner must not load production config'))
    with pytest.raises(SystemExit) as error:
        command(monkeypatch, capsys, 'inspect')
    assert error.value.code == 2


@pytest.mark.parametrize('command_name', ['show', 'submit', 'status'])
@pytest.mark.parametrize('case_id', ['../snapshot', '/etc/passwd', 'A' * 32, '0' * 31, '0' * 33])
def test_case_identifier_cannot_access_arbitrary_state(command_name, case_id, campaign, monkeypatch, capsys):
    path, trader, venue = campaign
    before = material(path)
    with pytest.raises(ValueError): command(monkeypatch, capsys, command_name, case_id)
    assert material(path) == before and trader.halted and venue.sent == []


@pytest.mark.parametrize('field,value', [('fingerprint', 'b' * 64), ('account_index', 101), ('approved', False)])
def test_wrong_activation_identity_blocks_inspection(campaign, monkeypatch, capsys, field, value):
    path, trader, venue = campaign
    approval = json.loads((path / 'activation.json').read_text())
    approval[field] = value
    (path / 'activation.json').write_text(json.dumps(approval))
    with pytest.raises(RuntimeError, match='activation'):
        command(monkeypatch, capsys, 'inspect')
    assert not (path / 'safety-resolution').exists() and venue.sent == []
