"""Actual bootstrap source-copy attribution survives a first-party value winner."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from apps.api.main import create_app
from cli.acceptance_public_source import _public_jobs
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source
from tests.integration.test_collection_inventory_proofs import staged


def _attributed_case(staged):
    original = staged._greenhouse_board
    def board(suffix, params=None):
        result = original(suffix, params)
        if suffix:
            result['jobs'][0]['title'] = 'Verified Native Platform Engineer'
        return result
    staged._greenhouse_board = Mock(side_effect=board)
    assert staged._execute()['status'] == 'COMPLETED'
    store = Store(staged.case.url)
    try:
        app = create_app(store, 'http://127.0.0.1:9', Path(staged.output_dir), operation_mode='FIXTURE')
        with TestClient(app) as api:
            def request(method, path, **kwargs):
                response = api.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
            yield dict(fixture=staged, store=store, app=app, api=api, request=request,
                trigger=staged.request['trigger_job_id'], source_id=staged.case.source_id,
                native_id=staged.case.rows[0]['id'])
    finally:
        store.engine.dispose()


@pytest.fixture
def attributed(staged):
    yield from _attributed_case(staged)


@pytest.fixture
def distinct_attributed():
    from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests
    from tests.integration.test_aggregator_bootstrap_worker import AggregatorBootstrapWorkerTests
    from packages.persistence.store import now
    from sqlalchemy import update
    original_setup = AggregatorBootstrapCollectionTests.setUp
    def setup(case):
        original_setup(case)
        case.rows[0]['id'] = 7331
        case.rows[0]['url'] = 'https://remotive.com/remote-jobs/software-dev/senior-python-engineer-7331'
    fixture = AggregatorBootstrapWorkerTests()
    try:
        with patch.object(AggregatorBootstrapCollectionTests, 'setUp', setup):
            fixture.setUp()
        with fixture.case.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == fixture.parent_lineage['parent_operation_id']).values(
                state='COMPLETED', completed_at=now()))
        yield from _attributed_case(fixture)
    finally:
        fixture.doCleanups()


def test_real_source_copy_filter_survives_canonical_primary_transition(attributed):
    case = attributed
    request, job_id = case['request'], case['trigger']
    job = request('GET', '/jobs/'+job_id, params={'profile_id': 'fixture'})
    assert job['attributes']['provider'] == 'greenhouse'
    primary = job['attributes']['source_id']
    assert primary != case['source_id']
    for source in (case['source_id'], primary):
        rows = request('GET', '/jobs', params={'profile_id': 'fixture', 'job_source_id': source})['jobs']
        assert [row['id'] for row in rows] == [job_id]
    unrelated = configure_source(case['store'], 'default', provider='wwr')
    assert request('GET', '/jobs', params={'profile_id': 'fixture', 'job_source_id': unrelated})['jobs'] == []
    assert request('GET', '/jobs', params={'profile_id': 'fixture', 'job_source_id': case['source_id'], 'title': 'No such title'})['jobs'] == []
    # Application-route provider semantics remain independent of collector source.
    assert [row['id'] for row in request('GET', '/jobs', params={'profile_id': 'fixture',
        'job_source_id': case['source_id'], 'provider': 'greenhouse'})['jobs']] == [job_id]


def test_public_observer_joins_source_copy_after_native_values_win(attributed):
    case = attributed
    source = next(row for row in case['request']('GET', '/sources')['sources'] if row['id'] == case['source_id'])
    operation = case['request']('GET', '/operations/'+case['fixture'].parent_lineage['parent_operation_id'])
    profile = case['request']('GET', '/profiles/fixture')
    observed = _public_jobs(case['request'], source, operation,
        dict(job_id='separate-selected-mock', profile_version=profile['version']), 'fixture', 'https://remotive.com/api/remote-jobs')
    assert [row['job_id'] for row in observed] == [case['trigger']]
    assert observed[0]['source_id'] == case['source_id']
    assert observed[0]['native_posting_id'] == 1001
    assert observed[0]['primary_provider'] == 'greenhouse'
    assert observed[0]['primary_source_id'] != case['source_id']


def test_legacy_primary_attribution_remains_visible_without_new_copy_rows(attributed):
    case = attributed
    with case['store'].engine.begin() as connection:
        job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == case['trigger'])).mappings().one())
        route = dict(connection.execute(select(m.routes).where(m.routes.c.job_id == case['trigger'], m.routes.c.provider == 'remotive')).mappings().one())
        match = dict(connection.execute(select(m.matches).where(m.matches.c.job_id == case['trigger'])).mappings().one())
        job.update(id='legacy-primary-job', requisition='legacy-primary', attributes={**job['attributes'],
            'source_id': case['source_id'], 'provider': 'remotive'})
        connection.execute(m.jobs.insert().values(**job))
        connection.execute(m.routes.insert().values({**route, 'id': 'legacy-primary-route',
            'job_id': job['id'], 'requisition': 'legacy-primary'}))
        connection.execute(m.matches.insert().values({**match, 'id': 'legacy-primary-match', 'job_id': job['id']}))
    rows = case['request']('GET', '/jobs', params={'profile_id': 'fixture', 'job_source_id': case['source_id']})['jobs']
    assert len(rows) == 2 and {row['id'] for row in rows} == {case['trigger'], 'legacy-primary-job'}
    native = case['request']('GET', '/jobs', params={'profile_id': 'fixture', 'job_source_id': case['source_id'],
        'provider': 'greenhouse'})['jobs']
    assert [row['id'] for row in native] == [case['trigger']]


def test_retained_copy_is_not_attributed_to_a_retargeted_collector_pointer(attributed):
    from sqlalchemy import update
    case = attributed
    unrelated = configure_source(case['store'], 'default', provider='wwr')
    with case['store'].engine.begin() as connection:
        source = connection.execute(select(m.company_sources.c.id).where(
            m.company_sources.c.collector_source_id == case['source_id'])).scalar_one()
        connection.execute(update(m.company_sources).where(m.company_sources.c.id == source).values(collector_source_id=unrelated))
    assert [row['id'] for row in case['request']('GET', '/jobs', params={
        'profile_id': 'fixture', 'job_source_id': case['source_id']})['jobs']] == [case['trigger']]
    assert case['request']('GET', '/jobs', params={'profile_id': 'fixture', 'job_source_id': unrelated})['jobs'] == []


def selected_mock_application(case):
    """Separate synthetic selected application; no employer submission or files."""
    from packages.persistence.store import now
    store = case['store']
    profile = store.profile('fixture')
    with store.engine.begin() as connection:
        job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == case['trigger'])).mappings().one())
        connection.execute(m.companies.insert().values(id='selected-fixture-company', name='Selected Fixture', domain=''))
        connection.execute(m.jobs.insert().values({**job, 'id': 'selected-fixture-job', 'company_id': 'selected-fixture-company',
            'requisition': 'selected-fixture', 'title': 'Selected Mock Application',
            'attributes': {**job['attributes'], 'fictional': True, 'provider': 'mock-ats'}}))
        connection.execute(m.routes.insert().values(id='selected-fixture-route', job_id='selected-fixture-job',
            company_id='selected-fixture-company', provider='mock-ats', tenant='example', requisition='selected-fixture',
            url='http://127.0.0.1:9/apply'))
        connection.execute(m.applications.insert().values(id='selected-fixture-application', job_id='selected-fixture-job',
            route_id='selected-fixture-route', profile_id='fixture', profile_version=profile['version'], state='DRAFT',
            target='MOCK', fact_ids=[], trace={}, created_at=now()))
    return dict(source={'commit': 'a'*40}, profile_id='fixture', application_id='selected-fixture-application',
        operation_id='selected-mock-operation', request_id='selected-mock-request',
        selected_application_job_id='selected-fixture-job', selected_application_route_id='selected-fixture-route',
        external_submission_enabled=False)


@pytest.mark.parametrize('change', ['company-owner', 'failed-observation'])
def test_inconsistent_capture_cannot_lend_collector_attribution(attributed, change):
    from sqlalchemy import update
    case = attributed
    with case['store'].engine.begin() as connection:
        if change == 'company-owner':
            connection.execute(m.companies.insert().values(id='foreign-owner', name='Other Employer', domain=''))
            connection.execute(update(m.company_sources).where(m.company_sources.c.collector_source_id == case['source_id']).values(
                company_id='foreign-owner'))
        else:
            connection.execute(update(m.source_quality_observations).where(
                m.source_quality_observations.c.configured_source_id == case['source_id']).values(succeeded=False))
    assert case['request']('GET', '/jobs', params={'profile_id': 'fixture', 'job_source_id': case['source_id']})['jobs'] == []
    assert [row['id'] for row in case['request']('GET', '/jobs', params={'profile_id': 'fixture'})['jobs']] == [case['trigger']]


@pytest.mark.parametrize('change', ['native-id', 'company', 'company-label', 'source', 'storage-id', 'posting-id', 'foreign-origin', 'hash', 'missing-proof'])
def test_retained_remotive_join_preserves_source_identity_negatives(attributed, change):
    from cli.acceptance_public_browser import _join_postings
    from cli.acceptance_public_source import _digest
    case = attributed
    source = next(row for row in case['request']('GET', '/sources')['sources'] if row['id'] == case['source_id'])
    operation = case['request']('GET', '/operations/'+case['fixture'].parent_lineage['parent_operation_id'])
    profile = case['request']('GET', '/profiles/fixture')
    observation = _public_jobs(case['request'], source, operation,
        dict(job_id='separate-mock', profile_version=profile['version']), 'fixture', 'https://remotive.com/api/remote-jobs')[0]
    job = case['request']('GET', '/jobs/'+case['trigger'], params={'profile_id': 'fixture'})
    posting = next(row for row in job['postings'] if row['source'] == 'https://remotive.com/api/remote-jobs')
    if change == 'native-id': observation['native_posting_id'] = 7331
    elif change == 'company': job['company_id'] = 'f'*64
    elif change == 'company-label': job['company'] = 'Other Employer'
    elif change == 'source': observation['source_id'] = 'e'*64
    elif change == 'storage-id': posting['source_job_id'] = '1001'
    elif change == 'posting-id': posting['id'] = 'd'*64
    elif change == 'hash': posting['raw']['title'] = 'Unproven changed title'
    elif change == 'missing-proof':
        observation['postings'].append({**deepcopy(observation['postings'][0]), 'posting_id': 'absent-copy'})
    else:
        # Even matching recomputed snapshot hashes cannot admit a foreign origin.
        posting['raw']['url'] = 'https://foreign.example/jobs/1001'
        checksum = _digest(posting['raw'])
        observation['postings'][0]['raw_sha256'] = checksum
        for snapshot in posting['snapshots']: snapshot['sha256'] = checksum
        for snapshot in observation['postings'][0]['snapshots']: snapshot['sha256'] = checksum
    with pytest.raises(ValueError):
        _join_postings(job, observation, 'remotive')


def test_retained_native_id_is_source_specific_after_distinct_id_transition(distinct_attributed):
    from cli.acceptance_public_browser import _join_postings
    case = distinct_attributed
    source = next(row for row in case['request']('GET', '/sources')['sources'] if row['id'] == case['source_id'])
    operation = case['request']('GET', '/operations/'+case['fixture'].parent_lineage['parent_operation_id'])
    profile = case['request']('GET', '/profiles/fixture')
    observation = _public_jobs(case['request'], source, operation,
        dict(job_id='separate-mock', profile_version=profile['version']), 'fixture', 'https://remotive.com/api/remote-jobs')[0]
    job = case['request']('GET', '/jobs/'+case['trigger'], params={'profile_id': 'fixture'})
    assert job['attributes']['native_posting_id'] == 1001
    assert observation['native_posting_id'] == 7331
    assert _join_postings(job, observation, 'remotive')
