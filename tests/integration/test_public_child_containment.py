"""Actual persisted child family joined to injected local containment boundaries."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update

from apps.api.main import create_app
from apps.api.collection_children import collection_children
from cli.acceptance_containment import ContainmentError
from cli.acceptance_public_source import child_containment_context
from cli.acceptance_source_containment import OwnedPublicSourceContainment
from packages.persistence import models as m
from packages.persistence.store import now
from workers.dispatch import enqueue_operation


class LocalAPI:
    """Real API/storage except the selected fixture app and no-service health."""
    def __init__(self, store, output_dir):
        self.store = store
        self.client = TestClient(create_app(store, 'http://127.0.0.1:8081', output_dir,
                                           operation_mode='FIXTURE'))
        self.calls = []
        self.app = dict(id='application', profile_id='fixture', job_id='fixture-job',
                        route_id='fixture-route', target='MOCK', confirmation={'receipt': 'mock'})

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if method == 'GET' and path in {'/health', '/settings'}:
            return dict(operation_mode='FIXTURE', external_submission_enabled=False)
        if method == 'GET' and path == '/profiles/fixture/applications/application':
            return deepcopy(self.app)
        if method == 'GET' and path == '/jobs/fixture-job':
            return dict(id='fixture-job', attributes={'fictional': True},
                        routes=[dict(id='fixture-route', provider='mock-ats')])
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()


@pytest.fixture
def joined(request):
    from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests
    from tests.integration.test_aggregator_bootstrap_worker import AggregatorBootstrapWorkerTests
    from tests.unit.test_acceptance_containment import OwnedPublicContainmentTests
    boundary = OwnedPublicContainmentTests()
    boundary.setUp()
    boundary.now[0] = datetime.now(timezone.utc)
    worker = AggregatorBootstrapWorkerTests()
    state = {'boundary': boundary, 'worker': worker}
    state['new_primary'] = bool(getattr(request, 'param', False))
    original_parent = AggregatorBootstrapCollectionTests.parent_operation

    def begin_parent(case, **kwargs):
        api = LocalAPI(case.store, boundary.root / 'outputs' / 'api')
        state['api'] = api
        boundary.api = api
        baseline = dict(profile_id='fixture', profile_version=case.store.profile('fixture')['version'],
            application_id='application', selected_application_job_id='fixture-job',
            selected_application_route_id='fixture-route', confirmation={'receipt': 'mock'},
            external_submission_enabled=False, acceptance_run_id='child-containment-join')
        with patch('tests.unit.test_acceptance_containment.OwnedPublicContainment', OwnedPublicSourceContainment):
            owner = boundary.containment(baseline=baseline)
        state['owner'] = owner
        source = None
        if not state['new_primary']:
            with case.store.engine.connect() as connection:
                source = dict(connection.execute(select(m.job_sources).where(
                    m.job_sources.c.id == case.source_id)).mappings().one())
        state['context'] = dict(provider='remotive', base_url=owner.base_url,
            acceptance_run_id=baseline['acceptance_run_id'], source_id=source['id'] if source else None,
            source_policy_sha256=owner.policy_sha256(source) if source else None, newly_created=False)
        owner('preflight', state['context'])
        if state['new_primary']:
            owner('enable', state['context'])
            policy = dict(provider='remotive', tenant='default', enabled=True,
                poll_interval=604800, concurrency=1, request_timeout=10, rate_limit=1, jitter_seconds=0)
            case.source_id = api.request('PUT', '/sources', json=policy)['id']
            source = next(row for row in api.request('GET', '/sources')['sources'] if row['id'] == case.source_id)
            state['context'].update(source_id=source['id'], source_policy_sha256=owner.policy_sha256(source), newly_created=True)
            state['primary_policy'] = policy
            owner('dispatch', state['context'])
        return original_parent(case, **kwargs)

    try:
        configure = patch('tests.integration.test_aggregator_bootstrap_collection.configure_source',
                          return_value='not-configured-yet') if state['new_primary'] else nullcontext()
        with configure, patch.object(AggregatorBootstrapCollectionTests, 'parent_operation', begin_parent):
            worker.setUp()
        with worker.case.store.engine.connect() as connection:
            parent = connection.execute(select(m.operations).where(
                m.operations.c.id == worker.parent_lineage['parent_operation_id'])).mappings().one()
        state['context'].update(collection_operation_id=parent['id'],
            collection_request_id=parent['params']['request_id'],
            collection_operation_attempt=None, child_discovery_requested=False,
            owned_child_operation_ids=[], owned_child_request_ids=[],
            owned_child_lineage_sha256=None, owned_child_sources=[])
        yield state
    finally:
        if 'api' in state:
            state['api'].client.close()
        worker.doCleanups()
        boundary.doCleanups()


def finish_parent(state):
    worker = state['worker']
    with worker.case.store.engine.begin() as connection:
        connection.execute(update(m.operations).where(
            m.operations.c.id == worker.parent_lineage['parent_operation_id']).values(
                state='COMPLETED', completed_at=now()))


def current_context(state):
    worker = state['worker']
    lineage = worker.parent_lineage
    result = collection_children(worker.case.store, 'fixture', worker.case.source_id,
        lineage['parent_operation_id'], lineage['parent_operation_attempt'])
    evidence = dict(collection_operation_id=lineage['parent_operation_id'],
        collection_operation_attempt=lineage['parent_operation_attempt'],
        child_discovery=result)
    return {**state['context'], **child_containment_context(evidence)}


def test_running_parent_admits_only_its_actual_child_family(joined):
    owner, worker = joined['owner'], joined['worker']
    proof = owner('poll', joined['context'])
    assert proof['scheduler_contained'] is True
    assert any('/collections/' in path for _, path, _ in joined['api'].calls)
    enqueue_operation(worker.case.store, 'COMPANY_DISCOVER', 'unrelated-company',
                      dict(profile_id='fixture', discovery_request_id='unrelated-request'))
    with pytest.raises(ContainmentError):
        owner('poll', joined['context'])
    assert joined['boundary'].docker.unpause_calls == []
    assert owner.claim_path.exists()


def test_known_blocked_child_can_release_without_discovery_success(joined):
    owner, worker = joined['owner'], joined['worker']
    owner('poll', joined['context'])
    finish_parent(joined)
    with worker.case.store.engine.begin() as connection:
        connection.execute(update(m.operations).where(m.operations.c.id == worker.request['operation_id']).values(
            state='BLOCKED', error='ROBOTS_DENIED', completed_at=now()))
    context = current_context(joined)
    owner('finalize', context)
    receipt = owner('release', {**context, 'cleanup_status': 'NOT_REQUIRED'})
    assert receipt['released'] is True
    assert joined['boundary'].docker.unpause_calls == ['b' * 64]
    assert not any(method == 'PUT' for method, _, _ in joined['api'].calls)


@pytest.mark.parametrize('joined', [False, True], indirect=True)
def test_actual_enrollment_is_disabled_by_exact_audit_before_release(joined):
    owner, worker = joined['owner'], joined['worker']
    owner('poll', joined['context'])
    finish_parent(joined)
    outcome = worker._execute()
    assert outcome['status'] == 'COMPLETED', outcome
    context = current_context(joined)
    assert len(context['owned_child_sources']) == 1
    admitted = context['owned_child_sources'][0]
    owner('poll', context)
    owner('finalize', context)
    cleanup_status = 'NOT_REQUIRED'
    if joined['new_primary']:
        owner('cleanup', context)
        owner('cleanup', context)
        joined['api'].request('PUT', '/sources', json={**joined['primary_policy'], 'enabled': False})
        cleanup_status = 'RESTORED_DISABLED'
    # The route becomes strict409 once the proven-owned child source is disabled;
    # release must instead use frozen lineage plus live policies. Existing
    # primary sources never receive a helper cleanup callback.
    receipt = owner('release', {**context, 'cleanup_status': cleanup_status})
    assert receipt['released'] is True
    with worker.case.store.engine.connect() as connection:
        company = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == admitted['company_source_id'])).mappings().one()
        collector = connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == admitted['source_id'])).mappings().one()
        primary = connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == worker.case.source_id)).mappings().one()
    assert company['enabled'] is False and collector['enabled'] is False
    assert primary['enabled'] is (not joined['new_primary'])
    assert joined['boundary'].docker.unpause_calls == ['b' * 64]


def test_child_policy_drift_after_cleanup_retains_pause_and_claim(joined):
    owner, worker = joined['owner'], joined['worker']
    owner('poll', joined['context'])
    finish_parent(joined)
    assert worker._execute()['status'] == 'COMPLETED'
    context = current_context(joined)
    owner('finalize', context)
    admitted = context['owned_child_sources'][0]
    with worker.case.store.engine.begin() as connection:
        connection.execute(update(m.job_sources).where(
            m.job_sources.c.id == admitted['source_id']).values(request_timeout=7))
    with pytest.raises(ContainmentError):
        owner('release', {**context, 'cleanup_status': 'NOT_REQUIRED'})
    assert joined['boundary'].docker.unpause_calls == []
    assert owner.claim_path.exists()
