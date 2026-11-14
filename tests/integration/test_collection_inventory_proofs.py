"""Native enumeration readback through actual controlled worker and reopened Store."""
from copy import deepcopy
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update

from apps.api.collection_children import create_collection_children_router
from packages.persistence import models as m
from packages.persistence.store import Store, now


@pytest.fixture
def staged():
    from tests.integration.test_aggregator_bootstrap_worker import AggregatorBootstrapWorkerTests
    fixture = AggregatorBootstrapWorkerTests()
    fixture.setUp()
    try:
        with fixture.case.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id ==
                fixture.parent_lineage['parent_operation_id']).values(state='COMPLETED', completed_at=now()))
        yield fixture
    finally:
        fixture.doCleanups()


def readback(fixture, *, attempt=1):
    # A new engine must reconstruct proof from durable rows, never worker memory.
    store = Store(fixture.case.url)
    try:
        app = FastAPI()
        app.include_router(create_collection_children_router(store))
        path = f'/profiles/fixture/sources/{fixture.case.source_id}/collections/'
        path += fixture.parent_lineage['parent_operation_id'] + f'/inventory-proofs?attempt={attempt}'
        with TestClient(app) as api:
            return api.get(path)
    finally:
        store.engine.dispose()


def test_real_worker_inventory_survives_reopen(staged):
    outcome = staged._execute()
    assert outcome['status'] == 'COMPLETED', outcome
    response = readback(staged)
    assert response.status_code == 200, response.text
    observation = response.json()['observations'][0]
    assert observation['status'] == 'AVAILABLE'
    proof = observation['proof']
    assert proof['inventory_complete'] is True
    assert proof['enumerated_count'] == proof['accepted_count'] == len(proof['manifest']) == 1
    assert proof['child_operation_id'] == staged.operation['id']
    assert proof['child_operation_attempt'] == 1
    from services.company_discovery.aggregator_bootstrap import canonical_digest
    assert proof['sha256'] == canonical_digest({k: v for k, v in proof.items() if k != 'sha256'})
    with staged.case.store.engine.connect() as connection:
        row = proof['manifest'][0]
        posting = connection.execute(select(m.postings).where(m.postings.c.id == row['posting_id'])).mappings().one()
        snapshot = connection.execute(select(m.posting_snapshots).where(m.posting_snapshots.c.id == row['snapshot_id'])).mappings().one()
        assert posting['job_id'] == row['canonical_job_id']
        assert snapshot['sha256'] == row['snapshot_sha256']
    assert 'PASS' not in response.text
    assert_read_only(staged, 'AVAILABLE')


@pytest.mark.parametrize('case', ['partial', 'rejected', 'entry-overflow', 'byte-overflow'])
def test_real_import_preserves_partial_or_capacity_evidence(staged, case):
    original = staged._greenhouse_board
    def board(suffix, params=None):
        payload = original(suffix, params)
        if suffix:
            if case == 'partial':
                payload['meta'] = {'total': 2}
            elif case == 'rejected':
                payload['jobs'].append({**staged.native_job, 'id': 1002, 'location': ['invalid']})
        return payload
    staged._greenhouse_board = Mock(side_effect=board)
    from services.discovery import source_collection_receipts as receipts
    from contextlib import nullcontext
    limit = {'entry-overflow': 'MAX_ENTRIES', 'byte-overflow': 'MAX_BYTES'}.get(case)
    with patch.object(receipts, limit, 0) if limit else nullcontext():
        outcome = staged._execute()
    assert outcome['status'] == 'COMPLETED', outcome
    response = readback(staged)
    assert response.status_code == 200, response.text
    observation = response.json()['observations'][0]
    proof = observation['proof']
    assert proof['inventory_complete'] is False
    assert proof['accepted_count'] == 1
    with staged.case.store.engine.connect() as connection:
        assert connection.execute(select(m.postings).where(
            m.postings.c.source == proof['endpoint'])).mappings().all()
    if limit:
        assert observation['status'] == 'UNAVAILABLE'
        assert observation['reason'] == receipts.CAPACITY_REASON
        assert proof['manifest'] is proof['manifest_sha256'] is None
    else:
        assert observation['status'] == 'AVAILABLE'
        assert proof['provider_complete_inventory'] is (case != 'partial')
        assert proof['enumerated_count'] == (2 if case == 'rejected' else 1)
        assert proof['rejected_count'] == (1 if case == 'rejected' else 0)


def native_audit(store):
    with store.engine.connect() as connection:
        return dict(connection.execute(select(m.audit).where(
            m.audit.c.action == 'SOURCE_COLLECTION_COMPLETE',
            m.audit.c.payload['native_inventory']['schema_version'].as_integer() == 1)).mappings().one())


@pytest.mark.parametrize('mutation', ['receipt-count', 'manifest', 'snapshot', 'child-attempt',
    'native-disabled', 'duplicate-audit', 'private-extension'])
def test_changed_receipt_snapshot_attempt_or_source_fails_closed(staged, mutation):
    from uuid import uuid4
    from services.company_discovery.aggregator_bootstrap import canonical_digest
    assert staged._execute()['status'] == 'COMPLETED'
    audit = native_audit(staged.case.store)
    receipt = deepcopy(audit['payload']['native_inventory'])
    with staged.case.store.engine.begin() as connection:
        if mutation == 'snapshot':
            connection.execute(update(m.posting_snapshots).where(
                m.posting_snapshots.c.id == receipt['manifest'][0]['snapshot_id']).values(raw={'changed': True}))
        elif mutation == 'child-attempt':
            connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(attempt_count=2))
        elif mutation == 'native-disabled':
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == receipt['collector_source_id']).values(enabled=False))
        elif mutation == 'duplicate-audit':
            connection.execute(m.audit.insert().values({**audit, 'id': uuid4().hex}))
        else:
            if mutation == 'receipt-count':
                receipt['enumerated_count'] += 1
                receipt['accepted_count'] += 1
            elif mutation == 'manifest':
                receipt['manifest'] = []
                receipt['manifest_sha256'] = canonical_digest([])
                receipt['duplicate_count'] = receipt['accepted_count']
                receipt['inventory_complete'] = False
            else:
                receipt['private_debug'] = 'PRIVATE_MARKER'
            receipt['sha256'] = canonical_digest({k: v for k, v in receipt.items() if k != 'sha256'})
            connection.execute(update(m.audit).where(m.audit.c.id == audit['id']).values(
                payload={**audit['payload'], 'native_inventory': receipt}))
    response = readback(staged)
    assert response.status_code == 409, response.text
    assert response.json() == {'detail': 'COLLECTION_INVENTORY_PROOF_CHANGED'}


def test_wrong_parent_attempt_and_unsettled_child_do_not_inherit_proof(staged):
    response = readback(staged)
    assert response.status_code == 200, response.text
    assert response.json()['observations'][0]['reason'] == 'CHILD_NOT_COMPLETED'
    assert staged._execute()['status'] == 'COMPLETED'
    response = readback(staged, attempt=2)
    assert response.status_code == 409, response.text


@pytest.mark.parametrize('revocation', ['cancel', 'profile'])
def test_revocation_during_native_fetch_does_not_commit_import_or_receipt(staged, revocation):
    original = staged._greenhouse_board
    def board(suffix, params=None):
        payload = original(suffix, params)
        if suffix:
            if revocation == 'cancel':
                with staged.case.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == staged.operation['id']).values(cancel_requested=True))
            else:
                profile = staged.case.store.profile('fixture')
                profile['name'] = 'Changed during native fetch'
                staged.case.store.save_profile(profile, expected_version=profile['version'], require_expected_version=True)
        return payload
    staged._greenhouse_board = Mock(side_effect=board)
    outcome = staged._execute()
    assert staged._greenhouse_board.call_count >= 2
    assert outcome['status'] != 'COMPLETED', outcome
    with staged.case.store.engine.connect() as connection:
        assert not [row for row in connection.execute(select(m.audit)).mappings()
            if row['payload'].get('native_inventory')]
        assert not connection.execute(select(m.postings).where(
            m.postings.c.source == 'https://boards-api.greenhouse.io/v1/boards/acme/jobs')).all()


def test_committed_receipt_requires_worker_completion_and_readback_never_writes(staged):
    from services.discovery.sources.collect import collect_source
    def committed_then_exit(*args, **kwargs):
        collect_source(*args, **kwargs)
        raise SystemExit('controlled postcommit exit')
    with patch('services.discovery.sources.collect.collect_source', committed_then_exit):
        with pytest.raises(SystemExit):
            staged._execute()
    assert native_audit(staged.case.store)['payload']['native_inventory']['inventory_complete'] is True
    assert_read_only(staged, 'UNAVAILABLE')


def assert_read_only(staged, expected_status):
    from sqlalchemy import event
    statements = []
    def read_only(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        assert statement.lstrip().split()[0].upper() in {'SELECT', 'BEGIN'}
        assert 'FOR UPDATE' not in statement.upper()
    # A reader uses a fresh engine; exercise the function on an observed reopened
    # engine too, proving all validation stays in its single read snapshot.
    from apps.api.collection_inventory_proofs import collection_inventory_proofs
    reopened = Store(staged.case.url)
    event.listen(reopened.engine, 'before_cursor_execute', read_only)
    try:
        with patch.object(reopened, 'read_snapshot', wraps=reopened.read_snapshot) as snapshot, \
             patch.object(reopened, '_audit', side_effect=AssertionError('write')), \
             patch('socket.create_connection', side_effect=AssertionError('network')), \
             patch('workers.dispatch.enqueue_operation', side_effect=AssertionError('dispatch')):
            result = collection_inventory_proofs(reopened, 'fixture', staged.case.source_id,
                staged.parent_lineage['parent_operation_id'], 1)
        assert snapshot.call_count == 1
        assert result['observations'][0]['status'] == expected_status
        if expected_status == 'UNAVAILABLE':
            assert result['observations'][0]['reason'] == 'CHILD_NOT_COMPLETED'
        assert statements
    finally:
        event.remove(reopened.engine, 'before_cursor_execute', read_only)
        reopened.engine.dispose()
