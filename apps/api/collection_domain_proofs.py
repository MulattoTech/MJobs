"""Sanitized persisted domain corroboration for one exact collection attempt.

This is a database read model, not an acceptance result or a network receipt.
``execution_scope`` is metadata observed in the persisted resolver receipt; this
reader does not independently attest that public HTTP occurred. In particular,
controlled transport evidence remains controlled. Child completion and a
connected, same-employer journey must be established separately.
"""
from datetime import datetime

from sqlalchemy import select

from apps.api.collection_children import _collection_children_on, _one, _require
from packages.persistence import models as m
from services.company_discovery.aggregator_bootstrap import canonical_digest
from services.company_discovery.domains import normalize_domain
from services.company_discovery.requests import _current_aggregator_bootstrap, _fixture


def _domain_proof_on(connection, child):
    """Rebuild durable authority without authorizing, dispatching or fetching."""
    if not child['operation_id']:
        return None, 'DISCOVERY_OPERATION_UNAVAILABLE'
    request = _one(connection, m.company_discovery_requests, child['request_id'])
    operation = _one(connection, m.operations, child['operation_id'])
    if request['authorization'] not in {
            'AGGREGATOR_BOOTSTRAP_PENDING', 'AGGREGATOR_BOOTSTRAP_VERIFIED'}:
        return None, 'DOMAIN_CORROBORATION_UNAVAILABLE'
    context = _current_aggregator_bootstrap(
        connection, operation, request, fixture=_fixture(connection))
    receipt = context.get('bootstrap_receipt')
    if receipt is None:
        return None, 'BOOTSTRAP_RECEIPT_NOT_PERSISTED'
    domain_context = context.get('domain_context')
    if domain_context is None:
        return None, 'OFFICIAL_DOMAIN_NOT_ACQUIRED'
    pending = context['pending']
    envelope = context['bootstrap_envelope']
    domain_evidence = domain_context['evidence']
    _require(len(domain_evidence) == 1)
    domain = _one(connection, m.company_domains, domain_evidence[0]['domain_id'])
    _require(domain['company_id'] == child['company_id'] and domain['verified'] is True
        and domain['domain'] == domain_evidence[0]['domain']
        == normalize_domain(receipt['employer_url']))

    # A pre-existing verified domain need not be rewritten by acquisition. The
    # current request's receipt and acquisition audit establish corroboration;
    # the domain's original verification timestamp alone cannot do so.
    events = connection.execute(select(m.audit).where(
        m.audit.c.action == 'COMPANY_DOMAIN_ACQUIRED',
        m.audit.c.payload['request_id'].as_string() == request['id'],
        m.audit.c.payload['pending_sha256'].as_string() == pending['sha256'],
    ).order_by(m.audit.c.id).limit(2)).mappings().all()
    _require(len(events) <= 1)
    if not events:
        return None, 'DOMAIN_ACQUISITION_AUDIT_UNAVAILABLE'
    event = events[0]
    expected = dict(request_id=request['id'], method=receipt['schema'],
        pending_sha256=pending['sha256'], receipt_sha256=receipt['sha256'],
        source_snapshot_sha256=receipt['source_snapshot_sha256'],
        employer_response_sha256=receipt['job_response_sha256'],
        apply_response_sha256=receipt['apply_response_sha256'])
    if pending['version'] == 2:
        expected['verification_policy_sha256'] = pending['binding']['verification_policy_sha256']
    _require(event['entity_id'] == child['company_id'] and all(
        event['payload'].get(key) == value for key, value in expected.items()))
    acquired_at = datetime.fromisoformat(event['timestamp'])
    _require(acquired_at.tzinfo is not None)
    native = receipt['native']
    proof = dict(company_id=child['company_id'], request_id=request['id'],
        request_revision=request['revision'], operation_id=operation['id'],
        domain_id=domain['id'], domain=domain['domain'],
        domain_verification_method=domain['verification_method'],
        domain_evidence_sha256=domain_evidence[0]['sha256'],
        corroboration_method=expected['method'],
        execution_scope=receipt['execution_scope'],
        execution_scope_basis='PERSISTED_RECEIPT_METADATA',
        native={key: native[key] for key in ('provider', 'tenant', 'job_id',
            'native_response_sha256', 'native_json_sha256')},
        pending_sha256=pending['sha256'], receipt_sha256=receipt['sha256'],
        envelope_sha256=envelope['sha256'], raw_sha256=receipt['raw_sha256'],
        normalized_sha256=receipt['normalized_sha256'],
        source_snapshot_sha256=receipt['source_snapshot_sha256'],
        source_policy_sha256=receipt['source_policy_sha256'],
        employer_response_sha256=receipt['job_response_sha256'],
        apply_response_sha256=receipt['apply_response_sha256'],
        audit_id=event['id'], audit_sha256=canonical_digest(dict(event)),
        acquired_at=acquired_at.isoformat())
    if pending['version'] == 2:
        proof.update(verification_policy=pending['binding']['verification_policy'],
                     verification_policy_sha256=pending['binding']['verification_policy_sha256'])
    proof['sha256'] = canonical_digest(proof)
    return proof, None


def collection_domain_proofs(store, profile_id, source_id, parent_operation_id, attempt):
    """Observe at most two exact children in one read-only database snapshot.

    Missing persisted proof is explicitly unavailable. Inconsistent lineage
    raises rather than returning partial proof. As with the children endpoint,
    capture must precede policy cleanup: disabled or changed sources revoke the
    current readback. ``complete`` describes bounded intake, not acceptance.
    """
    with store.read_snapshot() as connection:
        collection = _collection_children_on(
            connection, profile_id, source_id, parent_operation_id, attempt)
        result = {key: collection[key] for key in ('schema_version', 'profile_id',
            'profile_version', 'source_id', 'parent_operation_id',
            'parent_operation_attempt', 'parent_request_id', 'complete', 'limit')}
        result.update(collection_lineage_sha256=collection['lineage_sha256'],
            collection_readback_sha256=collection['sha256'], observations=[])
        versions = set()
        for child in collection['children']:
            request = _one(connection, m.company_discovery_requests, child['request_id'])
            version = request['domain_evidence'][0]['version']
            _require(type(version) is int and version in (1, 2))
            versions.add(version)
            _require(len(versions) == 1)
            result['schema_version'] = version
            proof, reason = _domain_proof_on(connection, child)
            enrolled = [{key: row[key] for key in ('source_id', 'company_source_id',
                'operation_id', 'request_id', 'audit_id', 'audit_sha256',
                'pending_sha256', 'receipt_sha256', 'envelope_sha256')}
                for row in collection['enrolled_sources']
                if row['request_id'] == child['request_id']]
            result['observations'].append(dict(child=child,
                status='AVAILABLE' if proof is not None else 'UNAVAILABLE',
                reason=reason, proof=proof, enrolled_sources=enrolled))
        result['sha256'] = canonical_digest(result)
        return result
