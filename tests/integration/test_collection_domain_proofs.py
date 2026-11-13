"""Actual controlled reciprocal receipts exposed without acceptance promotion."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, select, update

from apps.api.collection_children import collection_children, create_collection_children_router
from apps.api.collection_domain_proofs import collection_domain_proofs
from packages.persistence import models as m
from packages.persistence.store import now
from services.company_discovery.aggregator_bootstrap import BudgetedPublicClient, canonical_digest
from services.company_discovery.aggregator_workflow import execute_aggregator_bootstrap
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.sources.greenhouse import Greenhouse
from workers.operations import claim


@pytest.fixture
def staged():
    # Reuse actual collector admission, not manually authored proof rows. The
    # fixture owns a temporary SQLite database and all transport is controlled.
    from tests.integration.test_aggregator_bootstrap_worker import AggregatorBootstrapWorkerTests

    fixture = AggregatorBootstrapWorkerTests()
    try:
        fixture.setUp()
        store = fixture.case.store
        parent = fixture.parent_lineage
        with store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == parent['parent_operation_id']).values(
                    state='COMPLETED', completed_at=now()))
        app = FastAPI()
        app.include_router(create_collection_children_router(store))
        with TestClient(app) as client:
            yield dict(fixture=fixture, store=store, client=client,
                args=(store, 'fixture', fixture.case.source_id,
                    parent['parent_operation_id'], parent['parent_operation_attempt']),
                path=f'/profiles/fixture/sources/{fixture.case.source_id}/collections/'
                    f'{parent["parent_operation_id"]}/domain-proofs?attempt=1')
    finally:
        fixture.doCleanups()


@pytest.fixture
def acquired(staged):
    fixture, store = staged['fixture'], staged['store']
    operation = claim(store, fixture.request['operation_id'])
    with patch.object(PublicCompanyClient, '_request_once', fixture._public_request), \
         patch.object(Greenhouse, '_get', fixture._greenhouse_board):
        execute_aggregator_bootstrap(store, operation, check=lambda: None,
            total_timeout=30, resolver_client_factory=BudgetedPublicClient)
    with store.engine.connect() as connection:
        request = dict(connection.execute(select(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == fixture.request['id'])).mappings().one())
        audit = dict(connection.execute(select(m.audit).where(
            m.audit.c.action == 'COMPANY_DOMAIN_ACQUIRED')).mappings().one())
        domain = dict(connection.execute(select(m.company_domains)).mappings().one())
    return dict(staged, request=request, audit=audit, domain=domain)


def _get(case):
    return case['client'].get(case['path'])


def _assert_unavailable(response, reason):
    assert response.status_code == 200, response.text
    observation = response.json()['observations'][0]
    assert observation['status'] == 'UNAVAILABLE'
    assert observation['reason'] == reason
    assert observation['proof'] is None


def test_exact_persisted_domain_receipt_and_audit_stay_controlled(acquired):
    response = _get(acquired)
    assert response.status_code == 200, response.text
    result = response.json()
    children = collection_children(*acquired['args'])
    assert result['collection_lineage_sha256'] == children['lineage_sha256']
    assert result['collection_readback_sha256'] == children['sha256']
    assert result['complete'] is True and result['limit'] == 2
    assert len(result['observations']) == 1
    observation = result['observations'][0]
    assert observation['child'] == children['children'][0]
    # Service execution has persisted proof but has not completed the worker.
    assert observation['child']['state'] == 'RUNNING'
    assert observation['status'] == 'AVAILABLE' and observation['reason'] is None
    proof = observation['proof']
    request, audit = acquired['request'], acquired['audit']
    receipt = request['domain_evidence'][1]['receipt']
    assert proof['company_id'] == request['company_id'] == audit['entity_id']
    assert proof['request_id'] == request['id'] == audit['payload']['request_id']
    assert proof['operation_id'] == request['operation_id']
    assert proof['request_revision'] == request['revision']
    assert proof['domain'] == 'acme.example'
    assert proof['domain_verification_method'] == 'FIRST_PARTY_JOB_LINK'
    assert proof['execution_scope'] == receipt['execution_scope'] == 'CONTROLLED_TRANSPORT'
    assert proof['execution_scope_basis'] == 'PERSISTED_RECEIPT_METADATA'
    assert proof['native']['provider'] == 'greenhouse'
    assert proof['native']['tenant'] == 'acme' and proof['native']['job_id'] == '1001'
    assert proof['receipt_sha256'] == receipt['sha256']
    assert proof['employer_response_sha256'] == receipt['job_response_sha256']
    assert proof['apply_response_sha256'] == receipt['apply_response_sha256']
    assert proof['audit_id'] == audit['id']
    assert proof['audit_sha256'] == canonical_digest(audit)
    assert observation['enrolled_sources'][0]['request_id'] == request['id']
    for value in (proof, result):
        assert value['sha256'] == canonical_digest({k: v for k, v in value.items() if k != 'sha256'})
    assert 'acceptance_observations' not in result
    assert 'PASS' not in response.text and 'PUBLIC_READ_ONLY' not in response.text


def test_readback_is_one_snapshot_without_writes_dispatch_or_network(acquired):
    store = acquired['store']
    before = store.workspace_status()['fingerprint']
    statements = []

    def read_only(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        assert statement.lstrip().split()[0].upper() in {'SELECT', 'BEGIN'}
        assert 'FOR UPDATE' not in statement.upper()

    event.listen(store.engine, 'before_cursor_execute', read_only)
    try:
        with patch.object(store, 'read_snapshot', wraps=store.read_snapshot) as snapshot, \
             patch.object(PublicCompanyClient, '_request_once', side_effect=AssertionError('network')), \
             patch('socket.create_connection', side_effect=AssertionError('network')), \
             patch('workers.dispatch.enqueue_operation', side_effect=AssertionError('dispatch')), \
             patch.object(store, '_audit', side_effect=AssertionError('write')):
            result = collection_domain_proofs(*acquired['args'])
        assert snapshot.call_count == 1
        assert result['observations'][0]['status'] == 'AVAILABLE'
    finally:
        event.remove(store.engine, 'before_cursor_execute', read_only)
    assert statements
    assert store.workspace_status()['fingerprint'] == before


def test_projection_omits_raw_urls_html_text_and_private_audit_fields(acquired):
    marker = 'PRIVATE_APPLICANT_TOKEN <html> https://private.example/?secret=redacted'
    with acquired['store'].engine.begin() as connection:
        connection.execute(update(m.audit).where(m.audit.c.id == acquired['audit']['id']).values(
            payload={**acquired['audit']['payload'], 'private_debug': marker}))
    response = _get(acquired)
    assert response.status_code == 200, response.text
    serialized = response.text
    for value in ('https://', 'http://', '<html>', 'PRIVATE_APPLICANT_TOKEN', 'private_debug',
                  'company_name', 'normalized_projection', 'description', 'discovery_chain',
                  'employer_url', 'apply_url', acquired['fixture'].title, acquired['fixture'].company):
        assert value not in serialized


def test_pending_actual_collection_has_explicitly_unavailable_proof(staged):
    _assert_unavailable(_get(staged), 'BOOTSTRAP_RECEIPT_NOT_PERSISTED')


def test_completed_worker_retains_exact_controlled_corroboration(staged):
    outcome = staged['fixture']._execute()
    assert outcome['status'] == 'COMPLETED', outcome
    response = _get(staged)
    assert response.status_code == 200, response.text
    observation = response.json()['observations'][0]
    assert observation['child']['state'] == 'COMPLETED'
    assert observation['status'] == 'AVAILABLE'
    assert observation['proof']['execution_scope'] == 'CONTROLLED_TRANSPORT'


def test_existing_domain_timestamp_does_not_replace_current_receipt_and_audit(acquired):
    with acquired['store'].engine.begin() as connection:
        connection.execute(update(m.company_domains).where(
            m.company_domains.c.id == acquired['domain']['id']).values(
                last_verified_at='2000-01-01T00:00:00+00:00'))
    response = _get(acquired)
    assert response.status_code == 200, response.text
    proof = response.json()['observations'][0]['proof']
    assert proof['acquired_at'] == acquired['audit']['timestamp']
    assert proof['receipt_sha256'] == acquired['audit']['payload']['receipt_sha256']


@pytest.mark.parametrize('missing', ['domain-context', 'audit', 'audit-request', 'audit-pending'])
def test_missing_current_evidence_never_becomes_domain_proof(acquired, missing):
    with acquired['store'].engine.begin() as connection:
        if missing == 'domain-context':
            connection.execute(update(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == acquired['request']['id']).values(
                    domain_evidence=acquired['request']['domain_evidence'][:2]))
        elif missing == 'audit':
            connection.execute(delete(m.audit).where(m.audit.c.id == acquired['audit']['id']))
        else:
            key = 'request_id' if missing == 'audit-request' else 'pending_sha256'
            connection.execute(update(m.audit).where(m.audit.c.id == acquired['audit']['id']).values(
                payload={**acquired['audit']['payload'], key: 'other-request-or-pending'}))
    _assert_unavailable(_get(acquired), 'OFFICIAL_DOMAIN_NOT_ACQUIRED' if missing == 'domain-context'
        else 'DOMAIN_ACQUISITION_AUDIT_UNAVAILABLE')


@pytest.mark.parametrize('mutation', ['profile', 'attempt', 'disabled-source', 'raw', 'child-company',
    'child-request', 'receipt-scope', 'audit-company', 'audit-receipt', 'audit-response',
    'unverified-domain', 'ambiguous-domain', 'duplicate-audit', 'audit-time'])
def test_changed_or_cross_company_lineage_fails_closed(acquired, mutation):
    request, audit, domain = acquired['request'], acquired['audit'], acquired['domain']
    with acquired['store'].engine.begin() as connection:
        if mutation == 'profile':
            connection.execute(update(m.profiles).where(m.profiles.c.id == 'fixture').values(version='a' * 64))
        elif mutation == 'attempt':
            connection.execute(update(m.operations).where(m.operations.c.id == acquired['args'][3]).values(attempt_count=2))
        elif mutation == 'disabled-source':
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == acquired['args'][2]).values(enabled=False))
        elif mutation == 'raw':
            connection.execute(update(m.posting_snapshots).where(m.posting_snapshots.c.id ==
                request['domain_evidence'][0]['binding']['posting_snapshot_id']).values(raw={'changed': True}))
        elif mutation == 'child-company':
            connection.execute(update(m.operations).where(m.operations.c.id == request['operation_id']).values(entity_id='other-company'))
        elif mutation == 'child-request':
            operation = connection.execute(select(m.operations).where(m.operations.c.id == request['operation_id'])).mappings().one()
            connection.execute(update(m.operations).where(m.operations.c.id == request['operation_id']).values(
                params={**operation['params'], 'discovery_request_id': 'other-request'}))
        elif mutation == 'receipt-scope':
            evidence = deepcopy(request['domain_evidence'])
            evidence[1]['receipt']['execution_scope'] = 'PUBLIC_READ_ONLY'
            connection.execute(update(m.company_discovery_requests).where(m.company_discovery_requests.c.id == request['id']).values(domain_evidence=evidence))
        elif mutation == 'audit-company':
            connection.execute(update(m.audit).where(m.audit.c.id == audit['id']).values(entity_id='other-company'))
        elif mutation in {'audit-receipt', 'audit-response'}:
            key = 'receipt_sha256' if mutation == 'audit-receipt' else 'employer_response_sha256'
            connection.execute(update(m.audit).where(m.audit.c.id == audit['id']).values(payload={**audit['payload'], key: 'a' * 64}))
        elif mutation == 'unverified-domain':
            connection.execute(update(m.company_domains).where(m.company_domains.c.id == domain['id']).values(verified=False))
        elif mutation == 'ambiguous-domain':
            connection.execute(m.company_domains.insert().values({**domain, 'id': uuid4().hex, 'domain': 'other.example'}))
        elif mutation == 'audit-time':
            connection.execute(update(m.audit).where(m.audit.c.id == audit['id']).values(timestamp='<html>private</html>'))
        else:
            connection.execute(m.audit.insert().values({**audit, 'id': uuid4().hex}))
    response = _get(acquired)
    assert response.status_code == 409, response.text
    assert response.json() == {'detail': 'COLLECTION_DOMAIN_PROOF_CHANGED'}


def test_consistent_foreign_employer_receipt_cannot_certify_this_domain(acquired):
    # Rehash and relink a foreign employer URL to prove that a hash match alone
    # cannot join it to this company's persisted official domain.
    evidence = deepcopy(acquired['request']['domain_evidence'])
    envelope = evidence[1]
    receipt = envelope['receipt']
    receipt['employer_url'] = 'https://other.example/job/1001'
    receipt['native']['absolute_url'] = receipt['employer_url']
    receipt['apply_url'] = 'https://other.example/apply/1001'
    receipt['sha256'] = canonical_digest({k: v for k, v in receipt.items() if k != 'sha256'})
    envelope['sha256'] = canonical_digest({k: v for k, v in envelope.items() if k != 'sha256'})
    with acquired['store'].engine.begin() as connection:
        connection.execute(update(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == acquired['request']['id']).values(domain_evidence=evidence))
        for row in connection.execute(select(m.audit).where(m.audit.c.action.in_(
                ['COMPANY_DOMAIN_ACQUIRED', 'COMPANY_SOURCE_DISCOVERED']))).mappings().all():
            payload = dict(row['payload'])
            if row['action'] == 'COMPANY_DOMAIN_ACQUIRED':
                payload['receipt_sha256'] = receipt['sha256']
            else:
                payload.update(aggregator_receipt_sha256=receipt['sha256'],
                    aggregator_envelope_sha256=envelope['sha256'])
            connection.execute(update(m.audit).where(m.audit.c.id == row['id']).values(payload=payload))
    assert _get(acquired).status_code == 409


def test_production_app_registers_domain_proofs(staged, tmp_path):
    from apps.api.main import create_app

    app = create_app(staged['store'], 'http://127.0.0.1:8081', tmp_path / 'outputs', operation_mode='FIXTURE')
    with TestClient(app) as client:
        response = client.get(staged['path'])
    _assert_unavailable(response, 'BOOTSTRAP_RECEIPT_NOT_PERSISTED')
