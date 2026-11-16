"""Read-only same-employer evidence over the actual controlled worker and API."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import now


@pytest.fixture
def employer():
    from cli.acceptance_public_employer import begin
    from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests
    from tests.integration.test_aggregator_bootstrap_worker import AggregatorBootstrapWorkerTests
    original = AggregatorBootstrapCollectionTests.parent_operation
    initial = {}
    def parent(case, *args, **kwargs):
        with TestClient(create_app(case.store, 'http://127.0.0.1:9', Path('.'), operation_mode='FIXTURE')) as api:
            initial.update(begin(reader(api), 'fixture', case.store.profile('fixture')['version']))
        return original(case, *args, **kwargs)
    fixture = AggregatorBootstrapWorkerTests()
    with patch.object(AggregatorBootstrapCollectionTests, 'parent_operation', parent):
        fixture.setUp()
    try:
        with fixture.case.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id ==
                fixture.parent_lineage['parent_operation_id']).values(state='COMPLETED', completed_at=now()))
        original_board = fixture._greenhouse_board
        def board(suffix, params=None):
            result = original_board(suffix, params)
            if suffix:
                result['jobs'].append(dict(fixture.native_job, id=1002, title='Python SQL Data Engineer',
                    absolute_url='https://job-boards.greenhouse.io/acme/jobs/1002'))
            return result
        fixture._greenhouse_board = Mock(side_effect=board)
        assert fixture._execute()['status'] == 'COMPLETED'
        with TestClient(create_app(fixture.case.store, 'http://127.0.0.1:9', fixture.output_dir,
                                  operation_mode='FIXTURE')) as api:
            read = reader(api)
            source = next(row for row in read('GET', '/sources')['sources'] if row['id'] == fixture.case.source_id)
            prefix = '/profiles/fixture/sources/' + source['id'] + '/collections/' + fixture.parent_lineage['parent_operation_id']
            children = read('GET', prefix + '/children', params={'attempt': 1})
            operation = read('GET', '/operations/' + children['parent_operation_id'])
            evidence = dict(profile_id='fixture', profile_version=initial['profile_version'],
                source_id=source['id'], endpoint='https://remotive.com/api/remote-jobs',
                collection_operation_id=children['parent_operation_id'], collection_operation_attempt=1,
                collection_request_id=children['parent_request_id'], execution_scope='CONTROLLED_TRANSPORT',
                collection_started_at=operation['started_at'],
                child_discovery={**children, 'readback_sha256': children['sha256']},
                domain_proof_readback={})
            from cli.acceptance_public_source import _policy_fingerprint
            evidence['source_policy_sha256'] = _policy_fingerprint(source)
            from cli.acceptance_public_source import _observe_domain_proofs
            _observe_domain_proofs(read, evidence, source)
            yield fixture, read, source, initial, evidence
    finally:
        fixture.doCleanups()


def reader(api):
    def read(method, path, **kwargs):
        assert method == 'GET', 'Observer must not write'
        response = api.request(method, path, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()
    return read


def test_real_same_employer_chain_is_captured_without_live_credit(employer):
    from cli.acceptance_public_employer import capture, validate
    fixture, read, source, initial, evidence = employer
    before = fixture.case.store.profile('fixture')
    result = capture(read, evidence, source, initial)
    validate(result, evidence)
    assert result['status'] == 'COMPLETE'
    assert result['public_acceptance'] == 'NOT_RUN'
    assert result['selected']['trigger_job']['id'] == fixture.request['trigger_job_id']
    assert result['selected']['native_only_job']['id'] != fixture.request['trigger_job_id']
    assert result['selected']['inventory']['accepted_count'] == 2
    assert result['detail_reads'] <= 16
    assert fixture.case.store.profile('fixture') == before


def test_employer_only_job_must_be_absent_from_original_inventory(employer):
    """A native-only current posting is insufficient when the job predates the collection."""
    from cli.acceptance_public_employer import capture, validate
    from cli.acceptance_public_source import _digest
    _fixture, read, source, initial, evidence = employer
    first = capture(read, evidence, source, initial)
    assert first['status'] == 'COMPLETE'
    native_only_id = first['selected']['native_only_job']['id']

    # Model an unchanged historical job that also appears in the later native
    # collector manifest. The negative control must deny criterion 30 even if
    # all current postings on that job now point at the native endpoint.
    was_present = deepcopy(initial)
    was_present['baseline_job_ids'] = sorted(set(was_present.get('baseline_job_ids', [])) | {native_only_id})
    was_present['baseline_job_total'] = len(was_present['baseline_job_ids'])
    was_present['sha256'] = _digest({key: value for key, value in was_present.items() if key != 'sha256'})
    result = capture(read, evidence, source, was_present)
    validate(result, evidence)
    assert result['status'] == 'PARTIAL'
    assert result['selected'] is None
    assert result['public_acceptance'] == 'NOT_RUN'


def test_employer_baseline_retains_complete_bounded_original_inventory(employer):
    from cli.acceptance_public_employer import capture
    fixture, read, source, initial, evidence = employer
    result = capture(read, evidence, source, initial)
    assert initial['baseline_job_scope'] == 'ALL_PERSISTED_JOBS'
    assert initial['baseline_job_total'] == len(initial['baseline_job_ids'])
    assert initial['baseline_job_ids'] == sorted(set(initial['baseline_job_ids']))
    assert set(initial['baseline_aggregator_job_ids']).issubset(initial['baseline_job_ids'])
    assert fixture.request['trigger_job_id'] not in initial['baseline_job_ids']  # parent discovery ran later
    assert result['selected']['native_only_job']['id'] not in initial['baseline_job_ids']


@pytest.mark.parametrize('defect', [
    None, 'changed-snapshot', 'missing-cursor', 'short-page',
    'budget-exceeded', 'source-copy-hash', 'empty-tail', 'copy-count-mismatch',
])
def test_original_inventory_is_complete_cursor_bound_and_bounded(defect):
    """Synthetic page protocol exercises immutable original-snapshot admission."""
    from cli.acceptance_public_employer import _original_inventory
    from cli.acceptance_public_source import ObservationError
    from packages.persistence.job_inventory import digest as inventory_digest

    ids = [f'{index:064x}' for index in range(201)]
    source_copy = {'posting_id': 'old-aggregator-copy',
                   'source': 'https://remotive.com/api/remote-jobs',
                   'source_job_id': 'original-1', 'source_url': 'https://example.com/job/1',
                   'sha256': 'd' * 64}
    rows = []
    for i, job_id in enumerate(ids):
        copies = [source_copy] if i == 0 else []
        rows.append({'job_id': job_id, 'canonical_sha256': 'c' * 64,
                     'route_count': 0, 'source_copy_count': len(copies),
                     'source_copies': copies,
                     'source_copies_sha256': inventory_digest(copies)})

    requests = []
    def read(method, path, **kwargs):
        assert method == 'GET' and path == '/profiles/fixture/job-inventory'
        params = kwargs['params']
        requests.append(deepcopy(params))
        page_no = len(requests)
        assert params == ({'limit': 200} if page_no == 1 else
                          {'limit': 200, 'cursor': 'cursor-1', 'snapshot_sha256': 'a' * 64})
        chunk = deepcopy(rows[:200] if page_no == 1 else rows[200:])
        page = {'schema_version': 4, 'scope': 'ALL_PERSISTED_JOBS',
                'profile_id': 'fixture', 'profile_version': 'v1',
                'total': 201, 'source_copy_count': 1, 'snapshot_sha256': 'a' * 64,
                'after': None if page_no == 1 else ids[199],
                'jobs': chunk, 'complete': page_no == 2,
                'next_cursor': 'cursor-1' if page_no == 1 else None}
        if defect == 'changed-snapshot' and page_no == 2:
            page['snapshot_sha256'] = 'b' * 64
        elif defect == 'missing-cursor' and page_no == 1:
            page['next_cursor'] = None
        elif defect == 'short-page' and page_no == 1:
            page['jobs'] = chunk[:-1]
        elif defect == 'budget-exceeded' and page_no == 1:
            page['total'] = 1001
        elif defect == 'source-copy-hash' and page_no == 1:
            page['jobs'][0]['source_copies_sha256'] = '0' * 64
        elif defect == 'copy-count-mismatch':
            page['source_copy_count'] = 2
        elif defect == 'empty-tail':
            page['total'] = 200
            if page_no == 2:
                page['jobs'] = []
        return page

    if defect is None:
        snapshot = _original_inventory(read, 'fixture', 'v1')
        assert snapshot['job_ids'] == ids
        assert snapshot['aggregator_job_ids'] == [ids[0]]
        assert snapshot['snapshot_sha256'] == 'a' * 64
        assert len(requests) == 2
    else:
        errors = {
            'changed-snapshot': 'EMPLOYER_BASELINE_INVENTORY_CHANGED',
            'missing-cursor': 'EMPLOYER_BASELINE_INVENTORY_INCOMPLETE',
            'short-page': 'EMPLOYER_BASELINE_INVENTORY_INCOMPLETE',
            'budget-exceeded': 'EMPLOYER_BASELINE_INVENTORY_BUDGET_EXCEEDED',
            'source-copy-hash': 'EMPLOYER_BASELINE_INVENTORY_INVALID',
            'empty-tail': 'EMPLOYER_BASELINE_INVENTORY_INVALID',
            'copy-count-mismatch': 'EMPLOYER_BASELINE_INVENTORY_INCOMPLETE',
        }
        with pytest.raises(ObservationError, match=errors[defect]):
            _original_inventory(read, 'fixture', 'v1')


@pytest.mark.parametrize('mutation', ['company', 'parent-attempt', 'child-attempt', 'snapshot',
    'profile', 'inventory-count', 'scope', 'baseline', 'fragment'])
def test_replayed_or_mixed_employer_evidence_is_rejected(employer, mutation):
    from cli.acceptance_public_employer import capture, validate
    fixture, read, source, initial, evidence = employer
    result = capture(read, evidence, source, initial)
    selected = result['selected']
    if mutation == 'company':
        selected['source']['company_id'] = 'unrelated'
    elif mutation == 'parent-attempt':
        selected['inventory']['parent_operation_attempt'] += 1
    elif mutation == 'child-attempt':
        selected['inventory']['child_operation_attempt'] += 1
    elif mutation == 'snapshot':
        selected['trigger_job']['postings'][0]['snapshots'][0]['sha256'] = '0' * 64
    elif mutation == 'profile':
        selected['native_only_job']['match']['profile_version'] = '0' * 64
    elif mutation == 'inventory-count':
        selected['inventory']['accepted_count'] += 1
    elif mutation == 'scope':
        result['public_acceptance'] = 'PASS'
    elif mutation == 'baseline':
        result['initial']['company_ids'].append(selected['source']['company_id'])
    else:
        result['selected'] = {'domain': selected['domain']}
    # Rehashing a modified outer envelope does not make its inner joins valid.
    from cli.acceptance_public_source import _digest
    result['sha256'] = _digest({k: v for k, v in result.items() if k != 'sha256'})
    with pytest.raises(ValueError):
        validate(result, evidence)


@pytest.mark.parametrize('missing', ['already-known', 'no-native-only', 'irrelevant', 'partial-inventory'])
def test_missing_prerequisite_keeps_whole_chain_partial(employer, missing):
    from cli.acceptance_public_employer import capture, validate
    from cli.acceptance_public_source import _digest
    fixture, read, source, initial, evidence = employer
    if missing == 'already-known':
        initial = deepcopy(initial)
        initial['company_ids'].append(fixture.request['company_id'])
        initial['sha256'] = _digest({k: v for k, v in initial.items() if k != 'sha256'})
    def controlled_read(method, path, **kwargs):
        value = read(method, path, **kwargs)
        if path.endswith('/inventory-proofs') and missing == 'partial-inventory':
            proof = value['observations'][0]['proof']
            proof.update(provider_complete_inventory=False, inventory_complete=False)
            raw = {k: v for k, v in proof.items() if k not in {'audit_id', 'audit_sha256', 'receipt_sha256', 'sha256'}}
            proof['receipt_sha256'] = _digest(raw)
            proof['sha256'] = _digest({k: v for k, v in proof.items() if k != 'sha256'})
            value['sha256'] = _digest({k: v for k, v in value.items() if k != 'sha256'})
        elif path.startswith('/jobs/') and value['id'] != fixture.request['trigger_job_id']:
            if missing == 'irrelevant':
                value['match']['score'] = 1
            elif missing == 'no-native-only':
                value['postings'].append({**deepcopy(value['postings'][0]), 'id': 'another-source-copy',
                    'source': 'https://remotive.com/api/remote-jobs'})
        return value
    result = capture(controlled_read, evidence, source, initial)
    validate(result, evidence)
    assert result['status'] == 'PARTIAL'
    assert result['selected'] is None
    assert result['public_acceptance'] == 'NOT_RUN'


def controlled_join_fixture(employer):
    """Synthetic public metadata for decoder tests, NOT a live acceptance receipt."""
    from datetime import datetime, timedelta
    from cli import acceptance_journey as journey
    from cli.acceptance_public_employer import capture, browser_projection
    from cli.acceptance_public_source import _digest
    from packages.persistence.job_inventory import snapshot_digest
    from tests.unit.test_acceptance_journey import baseline, complete_inventory, public_browser_artifact, RUNTIME, RUN_ID
    from tests.unit.test_acceptance_employer_verification_opt_in import _controlled_ui_artifact
    fixture, read, source, initial, evidence = employer
    state = baseline()
    inventory = complete_inventory(1)
    inventory.update(profile_id='fixture', profile_version=initial['profile_version'])
    inventory['snapshot_sha256'] = snapshot_digest('fixture', initial['profile_version'], inventory['jobs'])
    state.update(_controlled_ui_artifact(profile=fixture.case.store.profile('fixture'),
        source=state['source'], run_id=RUN_ID, inventory=inventory))
    timestamp = (datetime.fromisoformat(initial['captured_at']) - timedelta(seconds=1)).isoformat()
    state['timestamp'] = timestamp
    state['employer_verification_opt_in'].update(saved_at_utc=timestamp, baseline_bound_at_utc=timestamp)
    public = {**deepcopy(evidence), **{key: state[key] for key in journey.IDENTIFIERS},
        'source': state['source'], 'acceptance_run_id': RUN_ID, 'timestamp': timestamp,
        'selected_application_job_id': state['selected_application_job_id'], 'selected_application_route_id': 'mock-route',
        'observed_real_job_ids': [fixture.request['trigger_job_id']], 'provider': 'remotive',
        'execution_scope': 'LIVE_PUBLIC_READ_ONLY', 'employer_verification_requested': True}
    domain = public['domain_proof_readback']['response']['observations'][0]['proof']
    domain['execution_scope'] = 'PUBLIC_READ_ONLY'
    domain['sha256'] = _digest({k: v for k, v in domain.items() if k != 'sha256'})
    response = public['domain_proof_readback']['response']
    response['sha256'] = _digest({k: v for k, v in response.items() if k != 'sha256'})
    public['domain_proof_readback']['response_sha256'] = response['sha256']
    public['employer_observation'] = capture(read, public, source, initial)
    browser = public_browser_artifact(state, public)
    browser['employer_observation'] = browser_projection(public['employer_observation'])
    browser['sha256'] = _digest({k: v for k, v in browser.items() if k != 'sha256'})
    return state, public, browser, RUNTIME


def test_whole_employer_group_joins_without_replacing_application_identity(employer):
    from cli import acceptance_journey as journey
    state, public, browser, runtime = controlled_join_fixture(employer)
    identity = journey._identity(state)
    joined = journey.join_public_browser(state, public, browser, 'a' * 64, 'b' * 64, runtime)
    segment_identity = journey.public_employer_identity(identity, joined)
    rows = journey.numbered_observations(joined, segment='public_browser', run_id=public['acceptance_run_id'],
        source=state['source'], identity=segment_identity)
    assert [row['step'] for row in rows] == [11, 16, *range(23, 32)]
    assert all(row['status'] == 'PASS' for row in rows)  # synthetic decoder contract only
    assert segment_identity['application_id'] == identity['application_id']
    assert segment_identity['job_id'] == identity['job_id']
    assert all('application_id' not in row['continuity'] for row in rows[2:])
    fragment = deepcopy(joined)
    fragment['acceptance_observations'] = [rows[4]]
    with pytest.raises(ValueError, match='complete same-employer group'):
        journey.numbered_observations(fragment, segment='public_browser', run_id=public['acceptance_run_id'],
            source=state['source'], identity=segment_identity)


def test_employer_join_rejects_missing_ui_scope_browser_and_chronology(employer):
    from cli import acceptance_journey as journey
    from cli.acceptance_public_source import _digest
    base_state, base_public, base_browser, runtime = controlled_join_fixture(employer)
    for mutation in ('opt-in', 'witness', 'browser-claim', 'browser-hash', 'policy', 'chronology', 'fragment'):
        state, public, browser = deepcopy(base_state), deepcopy(base_public), deepcopy(base_browser)
        if mutation == 'opt-in':
            public['employer_verification_requested'] = False
        elif mutation == 'witness':
            state.pop('employer_verification_opt_in')
        elif mutation == 'browser-claim':
            browser['employer_observation']['company_source_id'] = 'unrelated'
        elif mutation == 'browser-hash':
            browser['employer_observation']['capture_sha256'] = '0' * 64
        elif mutation == 'policy':
            state['employer_verification_opt_in']['policy_sha256'] = '0' * 64
        elif mutation == 'chronology':
            public['timestamp'] = journey.utc_now()
        else:
            browser['acceptance_observations'].append({'step': 25, 'status': 'PASS', 'checks': {}})
        browser['sha256'] = _digest({k: v for k, v in browser.items() if k != 'sha256'})
        with pytest.raises(ValueError):
            journey.join_public_browser(state, public, browser, 'a' * 64, 'b' * 64, runtime)


def test_controlled_domain_metadata_never_replaces_fixture_group(employer):
    from cli import acceptance_journey as journey
    from cli.acceptance_public_employer import capture, browser_projection
    from cli.acceptance_public_source import _digest
    fixture, read, source, initial, evidence = employer
    state, public, browser, runtime = controlled_join_fixture(employer)
    public['domain_proof_readback'] = deepcopy(evidence['domain_proof_readback'])
    public['employer_observation'] = capture(read, public, source, initial)
    browser['employer_observation'] = browser_projection(public['employer_observation'])
    browser['sha256'] = _digest({k: v for k, v in browser.items() if k != 'sha256'})
    joined = journey.join_public_browser(state, public, browser, 'a' * 64, 'b' * 64, runtime)
    assert [row['step'] for row in joined['acceptance_observations']] == [11, 16]
    assert 'employer_discovery_lineage' not in joined
