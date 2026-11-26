"""Regressions for exclusive-end history and one-request backlog starvation."""
import copy
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live.funding import funding_path, parse_funding, funding_amount
from live.reconcile_funding import reconcile
from test_paper_safety import broker, SYMBOL
from live.paper import PaperBroker

HOUR = 497613
ROW = {'timestamp': HOUR * 3600, 'rate': '0.0010', 'value': '0.00001422', 'direction': 'short'}


def event(hour=HOUR):
    return {'symbol': SYMBOL, 'settlement_hour': hour, 'side': 'SHORT', 'qty': 41775.,
            'reference_price': 1.422516, 'amount': None, 'status': 'pending_rate', 'next_retry': 0}


def test_exclusive_end_returns_exact_boundary_and_never_adjacent(tmp_path):
    b = broker(tmp_path)
    def endpoint(path):
        q = parse_qs(urlparse(path).query)
        assert q['count_back'] == ['0']
        end = int(q['end_timestamp'][0])
        return {'code': 200, 'fundings': [ROW] if end > ROW['timestamp'] else []}
    b._get = endpoint
    assert b._funding_rate_at(1, HOUR) == pytest.approx(-.001)
    assert b._funding_samples[f'1:{HOUR}']['raw'] == ROW
    assert b._funding_rate_at(1, HOUR + 1) is None


@pytest.mark.parametrize('payer,side,expected', [('short','SHORT',-.594256059),
    ('short','LONG',.594256059), ('long','SHORT',.594256059), ('long','LONG',-.594256059)])
def test_direction_and_percent_scale(payer, side, expected):
    row = {**ROW, 'direction': payer}
    e = {**event(), 'side': side}
    sample = parse_funding({'fundings':[row]}, HOUR)
    assert funding_amount(e, sample['fraction']) == pytest.approx(expected, abs=.000001)


@pytest.mark.parametrize('change', [{'rate':'nan'}, {'rate':'inf'}, {'rate':'-0.01'},
                                 {'direction':'unknown'}, {'direction':None}])
def test_invalid_contract_is_not_silently_booked(change):
    with pytest.raises(ValueError):
        parse_funding({'fundings':[{**ROW, **change}]}, HOUR)


def test_duplicates_and_failed_api_rejected():
    with pytest.raises(ValueError):
        parse_funding({'fundings':[ROW, ROW]}, HOUR)
    with pytest.raises(ValueError):
        parse_funding({'code':500, 'fundings':[ROW]}, HOUR)


def test_failed_oldest_does_not_starve_newer_and_retry_survives_reload(tmp_path):
    b = broker(tmp_path)
    b._last_funding_hour = HOUR + 2
    b._funding_events = {f'{SYMBOL}:{h}': event(h) for h in range(HOUR, HOUR+3)}
    calls = []
    b._funding_rate_at = lambda m,h: calls.append(h) or (None if h == HOUR else .001)
    for offset in (0, 61, 122):
        with patch('live.paper.time.time', return_value=(HOUR+2)*3600+offset):
            PaperBroker._accrue_funding(b, {})
    assert calls == [HOUR, HOUR+1, HOUR+2]
    assert b.funding_total == pytest.approx(2 * .594256059)
    b.save()
    restored = broker(tmp_path)
    restored._funding_rate_at = lambda m,h: None
    with patch('live.paper.time.time', return_value=(HOUR+2)*3600+183):
        PaperBroker._accrue_funding(restored, {})
    assert restored.cash == b.cash
    assert restored._funding_events[f'{SYMBOL}:{HOUR}']['status'] == 'missing_rate'


def test_lookup_failures_are_visible_and_recover(tmp_path):
    b = broker(tmp_path)
    b._get = lambda _: (_ for _ in ()).throw(OSError('offline'))
    assert b._funding_rate_at(1, HOUR) is None
    assert b._funding_samples[f'1:{HOUR}']['status'] == 'lookup_error'
    b._get = lambda _: {'fundings':[{**ROW,'direction':'bad'}]}
    assert b._funding_rate_at(1,HOUR) is None
    assert b._funding_samples[f'1:{HOUR}']['status'] == 'invalid_response'
    b._get = lambda _: {'fundings':[ROW]}
    assert b._funding_rate_at(1,HOUR) == -.001


def test_reconciliation_preserves_original_and_does_not_claim_corrected_mdd():
    report = {'mode':'paper', 'classification':'simulation_not_exchange_account',
        'open_positions':0, 'initial_cash':100000., 'cash':101865.19689338752,
        'funding_events':[event()], 'observation_coverage_complete':False,
        'risk':{'sampled_mdd_pct':-1.18}}
    before = copy.deepcopy(report)
    get = lambda _: {'fundings':[ROW]}
    out = reconcile(report, {SYMBOL:1}, get)
    assert out['funding_adjustment'] == pytest.approx(-.594256059)
    assert out['cash'] == pytest.approx(report['cash']-.594256059)
    assert out['funding_unresolved_count'] == 0
    assert out['corrected_mdd_pct'] is None and not out['performance_complete']
    assert report == before
    assert reconcile(report,{SYMBOL:1},get) == out
    with pytest.raises(ValueError):
        reconcile(out,{SYMBOL:1},get)
    report['funding_events'].append(event())
    with pytest.raises(ValueError):
        reconcile(report,{SYMBOL:1},get)


def test_reconciliation_keeps_missing_index_unresolved():
    report = {'mode':'paper','classification':'simulation_not_exchange_account',
        'open_positions':0,'initial_cash':1000,'cash':1000,
        'funding_events':[{**event(),'reference_price':None}]}
    out = reconcile(report,{SYMBOL:1},lambda _: pytest.fail('must not query'))
    assert out['funding_unresolved_count'] == 1 and out['funding_adjustment'] == 0

