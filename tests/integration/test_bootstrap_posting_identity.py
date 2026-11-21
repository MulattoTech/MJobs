"""Exact verified native identity attachment; existing ownership stays sticky."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, now
from tests.integration.test_collection_inventory_proofs import staged


def native_postings(store):
    with store.engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(m.postings).where(
            m.postings.c.source == 'https://boards-api.greenhouse.io/v1/boards/acme/jobs')).mappings()]


def test_exact_bootstrap_attaches_new_native_copy_preserving_original_application(staged):
    store = staged.case.store
    trigger = staged.request['trigger_job_id']
    with store.engine.begin() as connection:
        original_posting = dict(connection.execute(select(m.postings)).mappings().one())
        snapshots = [dict(row) for row in connection.execute(select(m.posting_snapshots)).mappings()]
        route = dict(connection.execute(select(m.routes).where(m.routes.c.job_id == trigger)).mappings().one())
        application = dict(id='preserved-application', job_id=trigger, route_id=route['id'], profile_id='fixture',
            profile_version=store.profile('fixture')['version'], state='DRAFT', target='MOCK',
            fact_ids=[], trace={}, created_at=now(), document_version=1)
        connection.execute(m.applications.insert().values(**application))
    original_board = staged._greenhouse_board
    def board(suffix, params=None):
        payload = original_board(suffix, params)
        if suffix:
            payload['jobs'].append(dict(staged.native_job, id=1002, title='Data Engineer',
                absolute_url='https://job-boards.greenhouse.io/acme/jobs/1002'))
        return payload
    staged._greenhouse_board = Mock(side_effect=board)
    outcome = staged._execute()
    assert outcome['status'] == 'COMPLETED', outcome
    postings = {row['source_job_id']: row for row in native_postings(store)}
    assert postings['1001']['job_id'] == trigger
    assert postings['1002']['job_id'] != trigger
    store.engine.dispose()
    reopened = Store(staged.case.url)
    try:
        with reopened.engine.connect() as connection:
            assert dict(connection.execute(select(m.postings).where(m.postings.c.id == original_posting['id'])).mappings().one()) == original_posting
            for snapshot in snapshots:
                assert dict(connection.execute(select(m.posting_snapshots).where(m.posting_snapshots.c.id == snapshot['id'])).mappings().one()) == snapshot
            assert dict(connection.execute(select(m.routes).where(m.routes.c.id == route['id'])).mappings().one()) == route
            actual_application = dict(connection.execute(select(m.applications).where(m.applications.c.id == application['id'])).mappings().one())
            assert all(actual_application[key] == value for key, value in application.items())
            assert len(connection.execute(select(m.jobs)).all()) == 2
        with TestClient(create_app(reopened, 'http://127.0.0.1:9', Path(staged.output_dir), operation_mode='FIXTURE')) as api:
            detail = api.get('/jobs/' + trigger + '?profile_id=fixture')
            assert detail.status_code == 200, detail.text
            assert {row['id'] for row in detail.json()['postings']} == {original_posting['id'], postings['1001']['id']}
            assert detail.json()['match']['profile_version'] == application['profile_version']
            assert detail.json()['attributes']['provider'] == 'greenhouse'
            path = f'/profiles/fixture/sources/{staged.case.source_id}/collections/'
            response = api.get(path + staged.parent_lineage['parent_operation_id'] + '/inventory-proofs?attempt=1')
            assert response.status_code == 200, response.text
            proof = response.json()['observations'][0]['proof']
            assert proof['inventory_complete'] is True and proof['accepted_count'] == 2
            assert {row['canonical_job_id'] for row in proof['manifest']} == {trigger, postings['1002']['job_id']}
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize('revocation', ['cancel', 'lease', 'attempt', 'params', 'profile', 'request-revision', 'company', 'receipt-tenant'])
def test_wrong_or_revoked_live_authority_cannot_attach(staged, revocation):
    original = staged._greenhouse_board
    def board(suffix, params=None):
        result = original(suffix, params)
        if suffix:
            store = staged.case.store
            if revocation == 'profile':
                profile = store.profile('fixture')
                profile['preferences']['company_discovery']['employer_verification']['mode'] = 'DISABLED'
                store.save_profile(profile, expected_version=profile['version'], require_expected_version=True)
            else:
                with store.engine.begin() as connection:
                    if revocation == 'cancel':
                        connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(cancel_requested=True))
                    elif revocation == 'lease':
                        connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(
                            lease_expires_at='2000-01-01T00:00:00+00:00'))
                    elif revocation == 'attempt':
                        connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(attempt_count=2))
                    elif revocation == 'params':
                        current = connection.execute(select(m.operations.c.params).where(m.operations.c.id == staged.operation['id'])).scalar_one()
                        connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(params={**current, 'unexpected': True}))
                    elif revocation == 'company':
                        connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(entity_id='other-company'))
                    elif revocation == 'request-revision':
                        connection.execute(update(m.company_discovery_requests).where(m.company_discovery_requests.c.id == staged.request['id']).values(
                            revision=staged.request['revision']+1))
                    else:
                        from services.company_discovery.aggregator_bootstrap import canonical_digest
                        request = connection.execute(select(m.company_discovery_requests).where(
                            m.company_discovery_requests.c.id == staged.request['id'])).mappings().one()
                        evidence = deepcopy(request['domain_evidence'])
                        envelope = evidence[1]
                        receipt = envelope['receipt']
                        receipt['native']['tenant'] = 'other'
                        receipt['sha256'] = canonical_digest({k: v for k, v in receipt.items() if k != 'sha256'})
                        envelope['sha256'] = canonical_digest({k: v for k, v in envelope.items() if k != 'sha256'})
                        connection.execute(update(m.company_discovery_requests).where(
                            m.company_discovery_requests.c.id == staged.request['id']).values(domain_evidence=evidence))
        return result
    staged._greenhouse_board = Mock(side_effect=board)
    outcome = staged._execute()
    assert outcome['status'] != 'COMPLETED', outcome
    assert staged._greenhouse_board.call_count >= 2
    assert native_postings(staged.case.store) == []
    with staged.case.store.engine.connect() as connection:
        trigger = connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == staged.request['trigger_job_id'])).scalar_one()
        assert trigger == staged.request['trigger_job_id']
        assert len(connection.execute(select(m.postings)).all()) == 1


def test_same_title_wrong_native_id_remains_separate(staged):
    original = staged._greenhouse_board
    def board(suffix, params=None):
        result = original(suffix, params)
        if suffix:
            result['jobs'] = [dict(staged.native_job, id=1002,
                absolute_url='https://job-boards.greenhouse.io/acme/jobs/1002')]
        return result
    staged._greenhouse_board = Mock(side_effect=board)
    outcome = staged._execute()
    assert outcome['status'] == 'COMPLETED', outcome
    rows = native_postings(staged.case.store)
    assert len(rows) == 1 and rows[0]['source_job_id'] == '1002'
    assert rows[0]['job_id'] != staged.request['trigger_job_id']


def expire_for_retry(staged):
    from workers.operations import recover_expired
    with staged.case.store.engine.begin() as connection:
        connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(
            lease_expires_at='2000-01-01T00:00:00+00:00'))
        connection.execute(update(m.job_sources).where(m.job_sources.c.provider == 'greenhouse').values(last_attempt=None))
    recover_expired(staged.case.store)


def test_recovery_is_idempotent_and_historical_native_identity_stays_sticky(staged):
    from services.discovery.sources.collect import collect_source
    from services.company_discovery.bootstrap_posting_identity import bootstrap_posting_job
    def committed_then_exit(*args, **kwargs):
        collect_source(*args, **kwargs)
        raise SystemExit('controlled postcommit exit')
    # Materialize the actual previous collector behavior as historical state,
    # using the real worker/importer with only the new fallback disabled.
    with patch('services.company_discovery.bootstrap_posting_identity.bootstrap_posting_job', return_value=None), \
         patch('services.discovery.sources.collect.collect_source', committed_then_exit):
        with pytest.raises(SystemExit):
            staged._execute()
    before = native_postings(staged.case.store)
    assert len(before) == 1 and before[0]['job_id'] != staged.request['trigger_job_id']
    with staged.case.store.engine.connect() as connection:
        routes_before = [dict(row) for row in connection.execute(select(m.routes)).mappings()]
        job_ids = set(connection.execute(select(m.jobs.c.id)).scalars())
    expire_for_retry(staged)
    with patch('services.company_discovery.bootstrap_posting_identity.bootstrap_posting_job', wraps=bootstrap_posting_job) as fallback:
        outcome = staged._execute()
    assert outcome['status'] == 'COMPLETED', outcome
    fallback.assert_not_called()
    after = native_postings(staged.case.store)
    assert [(row['id'], row['job_id'], row['source_job_id']) for row in after] == [
        (row['id'], row['job_id'], row['source_job_id']) for row in before]
    with staged.case.store.engine.connect() as connection:
        assert set(connection.execute(select(m.jobs.c.id)).scalars()) == job_ids
        assert [dict(row) for row in connection.execute(select(m.routes)).mappings()] == routes_before


@pytest.mark.parametrize('conflict', ['trigger-native-requisition', 'alternate-posting', 'foreign-native-route'])
def test_conflicting_existing_native_ownership_is_never_reparented(staged, conflict):
    store = staged.case.store
    trigger = staged.request['trigger_job_id']
    with store.engine.begin() as connection:
        job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == trigger)).mappings().one())
        if conflict == 'trigger-native-requisition':
            connection.execute(m.routes.insert().values(id='conflicting-native-route', job_id=trigger, company_id=job['company_id'],
                provider='greenhouse', tenant='acme', requisition='9999', url='https://job-boards.greenhouse.io/acme/jobs/9999'))
        else:
            company = job['company_id']
            if conflict == 'foreign-native-route':
                company = 'foreign-company'
                connection.execute(m.companies.insert().values(id=company, name='Other Employer', domain='other.example'))
            connection.execute(m.jobs.insert().values({**job, 'id': 'historical-native-job', 'company_id': company}))
            if conflict == 'alternate-posting':
                connection.execute(m.postings.insert().values(id='historical-native-posting', job_id='historical-native-job',
                    source='https://boards-api.greenhouse.io/v1/boards/acme/jobs', source_job_id='1001',
                    source_url='https://boards-api.greenhouse.io/v1/boards/acme/jobs', application_url='',
                    raw=staged.native_job, fetched_at=now()))
            else:
                connection.execute(m.routes.insert().values(id='foreign-native-route', job_id='historical-native-job',
                    company_id=company, provider='greenhouse', tenant='acme', requisition='1001',
                    url='https://job-boards.greenhouse.io/acme/jobs/1001'))
        postings_before = [dict(row) for row in connection.execute(select(m.postings)).mappings()]
        routes_before = [dict(row) for row in connection.execute(select(m.routes)).mappings()]
        job_ids_before = set(connection.execute(select(m.jobs.c.id)).scalars())
    outcome = staged._execute()
    assert outcome['status'] != 'COMPLETED', outcome
    with store.engine.connect() as connection:
        assert [dict(row) for row in connection.execute(select(m.postings)).mappings()] == postings_before
        assert [dict(row) for row in connection.execute(select(m.routes)).mappings()] == routes_before
        assert set(connection.execute(select(m.jobs.c.id)).scalars()) == job_ids_before


def test_import_rollback_then_real_recovery_attaches_once(staged):
    from services.company_discovery.bootstrap_posting_identity import bootstrap_posting_job
    chosen = []
    def observed_identity(*args, **kwargs):
        target = bootstrap_posting_job(*args, **kwargs)
        chosen.append(target['id'] if target else None)
        return target
    with staged.case.store.engine.connect() as connection:
        routes_before = [dict(row) for row in connection.execute(select(m.routes)).mappings()]
    with patch('services.company_discovery.bootstrap_posting_identity.bootstrap_posting_job', observed_identity), \
         patch('services.discovery.source_collection_receipts.build_native_inventory', side_effect=SystemExit('controlled precommit exit')):
        with pytest.raises(SystemExit):
            staged._execute()
    assert chosen == [staged.request['trigger_job_id']]
    assert native_postings(staged.case.store) == []
    with staged.case.store.engine.connect() as connection:
        assert [dict(row) for row in connection.execute(select(m.routes)).mappings()] == routes_before
        assert len(connection.execute(select(m.jobs)).all()) == 1
    expire_for_retry(staged)
    assert staged._execute()['status'] == 'COMPLETED'
    rows = native_postings(staged.case.store)
    assert len(rows) == 1 and rows[0]['job_id'] == staged.request['trigger_job_id']
    assert staged._execute()['status'] == 'ALREADY_CLAIMED_OR_CAPACITY'
    assert native_postings(staged.case.store) == rows


def test_distinct_aggregator_requisition_uses_exact_native_receipt_identity():
    from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests
    from tests.integration.test_aggregator_bootstrap_worker import AggregatorBootstrapWorkerTests
    original_setup = AggregatorBootstrapCollectionTests.setUp
    def source_setup(case):
        original_setup(case)
        case.rows[0]['id'] = 7331
        case.rows[0]['url'] = 'https://remotive.com/remote-jobs/software-dev/senior-python-engineer-7331'
    fixture = AggregatorBootstrapWorkerTests()
    try:
        with patch.object(AggregatorBootstrapCollectionTests, 'setUp', source_setup):
            fixture.setUp()
        with fixture.case.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == fixture.parent_lineage['parent_operation_id']).values(
                state='COMPLETED', completed_at=now()))
            original_job = dict(connection.execute(select(m.jobs)).mappings().one())
            original_route = dict(connection.execute(select(m.routes)).mappings().one())
        assert original_job['requisition'] == original_route['requisition'] == '7331'
        assert fixture._execute()['status'] == 'COMPLETED'
        native = native_postings(fixture.case.store)
        assert len(native) == 1 and native[0]['source_job_id'] == '1001'
        assert native[0]['job_id'] == original_job['id'] == fixture.request['trigger_job_id']
        with fixture.case.store.engine.connect() as connection:
            assert connection.execute(select(m.jobs.c.requisition).where(m.jobs.c.id == original_job['id'])).scalar_one() == '7331'
            assert dict(connection.execute(select(m.routes).where(m.routes.c.id == original_route['id'])).mappings().one()) == original_route
            alias = connection.execute(select(m.routes).where(m.routes.c.provider == 'greenhouse')).mappings().one()
            assert alias['job_id'] == original_job['id'] and alias['requisition'] == '1001'
    finally:
        fixture.doCleanups()
