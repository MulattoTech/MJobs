"""Read-only lineage projection over controlled persisted collection records."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select, update

from apps.api.collection_children import create_collection_children_router
from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.company_discovery.aggregator_bootstrap import canonical_digest, normalized_projection, source_policy_projection
from services.discovery.sources.collect import configure_source, collect_source
from tests.integration.test_spine import PROFILE
from tests.integration.test_wwr import RAW_WWR_RSS
from workers.dispatch import enqueue_operation


@pytest.fixture
def family(tmp_path):
    store = Store('sqlite:///' + (tmp_path / 'children.db').as_posix())
    store.migrate()
    store.bind_workspace_mode('FIXTURE')
    store.save_profile(deepcopy(PROFILE))
    profile = store.profile('fixture')
    source_id = configure_source(store, provider='wwr', tenant='default', enabled=True, poll_interval=300)
    if isinstance(source_id, dict): source_id = source_id['id']
    started = now()
    parent = enqueue_operation(store, 'DISCOVER', 'fixture', {
        'source_id': source_id, 'profile_id': 'fixture', 'profile_version': profile['version'], 'request_id': 'collection-request'})
    # These negative read-model fixtures author request mutations explicitly.
    # Actual collector admission is exercised separately below.
    with patch('services.discovery.sources.collect.record_discovery_requests'):
        collect_source(store, source_id, 'fixture', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=RAW_WWR_RSS)))
    with store.engine.begin() as connection:
        connection.execute(update(m.operations).where(m.operations.c.id == parent).values(
            state='COMPLETED', attempt_count=1, started_at=started, completed_at=now()))
    app = FastAPI()
    app.include_router(create_collection_children_router(store))
    client = TestClient(app)
    path = f'/profiles/fixture/sources/{source_id}/collections/{parent}/children?attempt=1'
    state = dict(store=store, profile=profile, source_id=source_id, parent=parent, client=client, path=path)
    yield state
    client.close()
    store.engine.dispose()


def child(family, index=0, state='RUNNING'):
    store = family['store']
    with store.engine.begin() as connection:
        candidate = connection.execute(select(m.posting_value_candidates).order_by(m.posting_value_candidates.c.id)).mappings().all()[index]
        posting = connection.execute(select(m.postings).where(m.postings.c.id == candidate['posting_id'])).mappings().one()
        snapshot = connection.execute(select(m.posting_snapshots).where(m.posting_snapshots.c.id == candidate['posting_snapshot_id'])).mappings().one()
        company_source = connection.execute(select(m.company_sources).where(m.company_sources.c.id == candidate['company_source_id'])).mappings().one()
        source = connection.execute(select(m.job_sources).where(m.job_sources.c.id == family['source_id'])).mappings().one()
        request_id = f'child-request-{index}'
        source_snapshot = {key: company_source[key] for key in ('company_id', 'provider', 'tenant', 'endpoint', 'collector_source_id')}
        projection = normalized_projection({**candidate['normalized'], 'application_url': candidate['application_url']})
        binding = dict(request_id=request_id, company_id=company_source['company_id'], request_revision=2,
            company_source_id=company_source['id'], candidate_id=candidate['id'], trigger_job_id=posting['job_id'],
            profile_id='fixture', profile_version=family['profile']['version'], posting_snapshot_id=snapshot['id'],
            quality_observation_id=candidate['quality_observation_id'], source_id=family['source_id'],
            source_snapshot=source_snapshot, source_snapshot_sha256=canonical_digest(source_snapshot),
            source_policy=source_policy_projection(source), source_policy_sha256=canonical_digest(source_policy_projection(source)),
            raw_sha256=snapshot['sha256'], normalized_sha256=canonical_digest(projection),
            selected_requisition=projection['requisition'], parent_operation_id=family['parent'], parent_operation_attempt=1)
        pending = dict(schema='AGGREGATOR_BOOTSTRAP_PENDING_V1', version=1, hint={}, binding=binding)
        pending['sha256'] = canonical_digest(pending)
        operation = enqueue_operation(None, 'COMPANY_DISCOVER', company_source['company_id'], {
            'profile_id': 'fixture', 'profile_version': family['profile']['version'],
            'discovery_request_id': request_id, 'discovery_request_revision': 2}, connection=connection)
        connection.execute(update(m.operations).where(m.operations.c.id == operation).values(
            state=state, attempt_count=1, started_at=now(), error='ROBOTS_DENIED' if state == 'BLOCKED' else None))
        connection.execute(m.company_discovery_requests.insert().values(id=request_id,
            company_id=company_source['company_id'], company_source_id=company_source['id'], candidate_id=candidate['id'],
            trigger_job_id=posting['job_id'], profile_id='fixture', profile_version=family['profile']['version'], reasons=[],
            created_at=now(), last_seen_at=now(), state='QUEUED', revision=2, authorization='AGGREGATOR_BOOTSTRAP_PENDING',
            domain_evidence=[pending], source_snapshot=source_snapshot, operation_id=operation))
    return request_id, operation


def test_no_children_is_complete_and_does_not_claim_discovery(family):
    response = family['client'].get(family['path'])
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['complete'] is True and value['children'] == []
    assert value['sha256'] == canonical_digest({key: value[key] for key in value if key != 'sha256'})


def test_exact_child_ids_are_joined_without_raw_payload_or_company_text(family):
    request, operation = child(family, state='BLOCKED')
    response = family['client'].get(family['path'])
    assert response.status_code == 200, response.text
    row = response.json()['children'][0]
    assert row['request_id'] == request and row['operation_id'] == operation
    assert row['state'] == 'BLOCKED' and row['reason'] == 'ROBOTS_DENIED'
    assert row['parent_operation_id'] == family['parent'] and row['parent_operation_attempt'] == 1
    assert 'raw' not in response.text and 'description' not in response.text and 'company_name' not in response.text


def test_running_parent_exposes_exact_children_without_claiming_complete_intake(family):
    request, _ = child(family)
    with family['store'].engine.begin() as connection:
        connection.execute(update(m.operations).where(m.operations.c.id == family['parent']).values(
            state='RUNNING', completed_at=None))
    response = family['client'].get(family['path'])
    assert response.status_code == 200, response.text
    assert response.json()['complete'] is False
    assert response.json()['children'][0]['request_id'] == request


@pytest.mark.parametrize('mutation', ['profile', 'attempt', 'source', 'raw', 'quality', 'child-op', 'pending-hash'])
def test_stale_or_forged_join_fails_closed(family, mutation):
    request, operation = child(family)
    with family['store'].engine.begin() as connection:
        row = connection.execute(select(m.company_discovery_requests).where(m.company_discovery_requests.c.id == request)).mappings().one()
        pending = deepcopy(row['domain_evidence'][0])
        if mutation == 'profile':
            connection.execute(update(m.profiles).where(m.profiles.c.id == 'fixture').values(version='a' * 64))
        elif mutation == 'attempt':
            connection.execute(update(m.operations).where(m.operations.c.id == family['parent']).values(attempt_count=2))
        elif mutation == 'source':
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == family['source_id']).values(enabled=False))
        elif mutation == 'raw':
            connection.execute(update(m.posting_snapshots).where(m.posting_snapshots.c.id == pending['binding']['posting_snapshot_id']).values(raw={'changed': True}))
        elif mutation == 'quality':
            connection.execute(update(m.source_quality_observations).where(m.source_quality_observations.c.id == pending['binding']['quality_observation_id']).values(succeeded=False))
        elif mutation == 'child-op':
            connection.execute(update(m.operations).where(m.operations.c.id == operation).values(entity_id='other-company'))
        else:
            pending['sha256'] = 'a' * 64
            connection.execute(update(m.company_discovery_requests).where(m.company_discovery_requests.c.id == request).values(domain_evidence=[pending]))
    assert family['client'].get(family['path']).status_code == 409


def test_more_than_two_children_is_explicitly_incomplete(family):
    for index in range(3): child(family, index=index)
    response = family['client'].get(family['path'])
    assert response.status_code == 200, response.text
    assert response.json()['complete'] is False
    assert len(response.json()['children']) <= 2


def test_actual_pending_verified_enrollment_audit_joins_exact_parent():
    from apps.api.collection_children import collection_children
    from services.company_discovery.aggregator_bootstrap import BudgetedPublicClient
    from services.company_discovery.aggregator_workflow import execute_aggregator_bootstrap
    from services.company_discovery.public_client import PublicCompanyClient
    from services.discovery.sources.greenhouse import Greenhouse
    from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests
    from tests.integration.test_aggregator_bootstrap_worker import AggregatorBootstrapWorkerTests
    from workers.operations import claim

    original_collect = AggregatorBootstrapCollectionTests.collect

    def parented_collect(case, *args, **kwargs):
        case.child_read_model_parent = case.parent_operation()
        result = original_collect(case, *args, **kwargs, **case.child_read_model_parent)
        with case.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == case.child_read_model_parent['parent_operation_id']).values(
                    state='COMPLETED', completed_at=now()))
        return result

    fixture = AggregatorBootstrapWorkerTests()
    try:
        with patch.object(AggregatorBootstrapCollectionTests, 'collect', parented_collect):
            fixture.setUp()
        store = fixture.case.store
        parent = fixture.case.child_read_model_parent
        args = (store, 'fixture', fixture.case.source_id,
                parent['parent_operation_id'], parent['parent_operation_attempt'])
        before = collection_children(*args)
        assert before['complete'] is True
        assert len(before['children']) == 1 and before['enrolled_sources'] == []
        assert before['children'][0]['authorization'] == 'AGGREGATOR_BOOTSTRAP_PENDING'
        operation = claim(store, fixture.request['operation_id'])
        with patch.object(PublicCompanyClient, '_request_once', fixture._public_request), \
             patch.object(Greenhouse, '_get', fixture._greenhouse_board):
            execute_aggregator_bootstrap(store, operation, check=lambda: None,
                total_timeout=30, resolver_client_factory=BudgetedPublicClient)
        after = collection_children(*args)
        assert after['lineage_sha256'] == before['lineage_sha256']
        assert after['children'][0]['authorization'] == 'AGGREGATOR_BOOTSTRAP_VERIFIED'
        assert len(after['enrolled_sources']) == 1
        enrolled = after['enrolled_sources'][0]
        assert enrolled['request_id'] == fixture.request['id']
        assert enrolled['operation_id'] == operation['id']
        assert enrolled['pending_sha256'] == before['children'][0]['pending_sha256']
        assert enrolled['policy']['provider'] == 'greenhouse'
        assert enrolled['policy']['tenant'] == 'acme'
        assert enrolled['policy_sha256'] == canonical_digest(enrolled['policy'])
        assert fixture.saw_unconfigured_source_before_first_fetch is True
        # Corrupting the real enrollment event must revoke the exposed source.
        with store.engine.begin() as connection:
            event = connection.execute(select(m.audit).where(m.audit.c.id == enrolled['audit_id'])).mappings().one()
            payload = {**event['payload'], 'aggregator_receipt_sha256': '0' * 64}
            connection.execute(update(m.audit).where(m.audit.c.id == event['id']).values(payload=payload))
        with pytest.raises(ValueError, match='COLLECTION_CHILD_LINEAGE_CHANGED'):
            collection_children(*args)
    finally:
        fixture.doCleanups()
