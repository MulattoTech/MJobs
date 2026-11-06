"""Bounded read model for children of one exact source-collection attempt."""
from datetime import datetime, timezone
import re

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.aggregator_bootstrap import (
    canonical_digest, normalized_projection, source_policy_projection,
)


SOURCE_FIELDS = ('company_id', 'provider', 'tenant', 'endpoint', 'collector_source_id')
PUBLIC_POLICY_FIELDS = ('id', 'provider', 'tenant', 'enabled', 'poll_interval', 'concurrency',
                        'request_timeout', 'rate_limit', 'jitter_seconds', 'strategy', 'capabilities')


def _require(condition):
    if not condition:
        raise ValueError('COLLECTION_CHILD_LINEAGE_CHANGED')


def _one(connection, table, identifier):
    row = connection.execute(select(table).where(table.c.id == identifier)).mappings().one_or_none()
    _require(row is not None)
    return row


def _code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', value) else (
        'UNCLASSIFIED_CHILD_FAILURE' if value else None)


def _enrolled_sources(connection, request, pending, operation):
    evidence = request['domain_evidence']
    events = connection.execute(select(m.audit).where(m.audit.c.action == 'COMPANY_SOURCE_DISCOVERED',
        m.audit.c.payload['discovery_request_id'].as_string() == request['id'],
        m.audit.c.payload['discovery_operation_id'].as_string() == operation['id']).order_by(m.audit.c.id).limit(3)).mappings().all()
    _require(len(events) <= 2)
    if not events:
        return []
    _require(len(evidence) >= 2)
    envelope = evidence[1]
    _require(isinstance(envelope, dict) and envelope.get('schema') == 'AGGREGATOR_NATIVE_BOOTSTRAP_ENVELOPE_V1'
        and envelope.get('pending_sha256') == pending['sha256']
        and envelope.get('sha256') == canonical_digest({key: value for key, value in envelope.items() if key != 'sha256'}))
    receipt = envelope['receipt']
    _require(receipt.get('sha256') == canonical_digest({key: value for key, value in receipt.items() if key != 'sha256'}))
    result = []
    for event in events:
        payload = event['payload']
        _require(payload.get('aggregator_pending_sha256') == pending['sha256']
            and payload.get('aggregator_receipt_sha256') == receipt['sha256']
            and payload.get('aggregator_envelope_sha256') == envelope['sha256'])
        source = _one(connection, m.company_sources, payload.get('company_source_id'))
        collector = _one(connection, m.job_sources, source['collector_source_id'])
        _require(source['company_id'] == request['company_id'] and source['discovered_automatically'] is True
            and source['enabled'] is True and collector['enabled'] is True
            and source['provider'] == collector['provider'] and source['tenant'] == collector['tenant']
            and source['provider'] == receipt['native']['provider'] and source['tenant'] == receipt['native']['tenant'])
        policy = {key: collector[key] for key in PUBLIC_POLICY_FIELDS}
        result.append(dict(source_id=collector['id'], company_source_id=source['id'],
            operation_id=operation['id'], request_id=request['id'], policy=policy,
            policy_sha256=canonical_digest(policy), audit_id=event['id'], audit_sha256=canonical_digest(dict(event)),
            pending_sha256=pending['sha256'], receipt_sha256=receipt['sha256'], envelope_sha256=envelope['sha256']))
    return result


def collection_children(store, profile_id, source_id, parent_operation_id, attempt):
    """No network, scheduling or writes; never infer parentage from nearby time."""
    _require(type(attempt) is int and attempt >= 1)
    with store.read_snapshot() as connection:
        parent = _one(connection, m.operations, parent_operation_id)
        source = _one(connection, m.job_sources, source_id)
        profile = _one(connection, m.profiles, profile_id)
        params = parent['params']
        _require(parent['kind'] == 'DISCOVER' and parent['entity_id'] == profile_id
            and parent['state'] in {'RUNNING', 'COMPLETED'} and type(parent['attempt_count']) is int and parent['attempt_count'] == attempt
            and isinstance(params, dict) and params.get('source_id') == source_id
            and params.get('profile_version') == profile['version'] and source['enabled'] is True
            and isinstance(params.get('request_id'), str) and params['request_id'])
        # Select by claimed exact parent alone, then validate every other join.
        # Filtering stale profile/source here could falsely report an empty set.
        rows = connection.execute(select(m.company_discovery_requests).where(
            m.company_discovery_requests.c.domain_evidence[0]['binding']['parent_operation_id'].as_string() == parent_operation_id
        ).order_by(m.company_discovery_requests.c.id).limit(3)).mappings().all()
        result = dict(schema_version=1, profile_id=profile_id, profile_version=profile['version'], source_id=source_id,
            parent_operation_id=parent_operation_id, parent_operation_attempt=attempt, parent_request_id=params['request_id'],
            complete=parent['state'] == 'COMPLETED' and len(rows) <= 2, limit=2, children=[], enrolled_sources=[])
        for row in rows[:2]:
            evidence = row['domain_evidence']
            _require(isinstance(evidence, list) and evidence)
            pending = evidence[0]
            _require(isinstance(pending, dict) and pending.get('schema') == 'AGGREGATOR_BOOTSTRAP_PENDING_V1'
                and type(pending.get('version')) is int and pending['version'] == 1
                and pending.get('sha256') == canonical_digest({key: value for key, value in pending.items() if key != 'sha256'}))
            binding = pending['binding']
            for key, value in dict(request_id=row['id'], request_revision=row['revision'], company_id=row['company_id'],
                company_source_id=row['company_source_id'], candidate_id=row['candidate_id'], trigger_job_id=row['trigger_job_id'],
                profile_id=profile_id, profile_version=profile['version'], source_id=source_id,
                parent_operation_id=parent_operation_id, parent_operation_attempt=attempt).items():
                _require(type(binding.get(key)) is type(value) and binding[key] == value)
            _require(row['profile_id'] == profile_id and row['profile_version'] == profile['version'])
            company_source = _one(connection, m.company_sources, row['company_source_id'])
            candidate = _one(connection, m.posting_value_candidates, row['candidate_id'])
            posting = _one(connection, m.postings, candidate['posting_id'])
            snapshot = _one(connection, m.posting_snapshots, candidate['posting_snapshot_id'])
            quality = _one(connection, m.source_quality_observations, candidate['quality_observation_id'])
            expected_source = {key: company_source[key] for key in SOURCE_FIELDS}
            expected_policy = source_policy_projection(source)
            projection = normalized_projection({**candidate['normalized'], 'application_url': candidate['application_url']})
            _require(company_source['enabled'] is True and company_source['company_id'] == row['company_id']
                and company_source['collector_source_id'] == source_id and candidate['company_source_id'] == company_source['id']
                and posting['job_id'] == row['trigger_job_id'] and snapshot['posting_id'] == posting['id']
                and binding['posting_snapshot_id'] == snapshot['id'] and binding['quality_observation_id'] == quality['id']
                and binding['source_snapshot'] == expected_source and row['source_snapshot'] == expected_source
                and binding['source_snapshot_sha256'] == canonical_digest(expected_source)
                and binding['source_policy'] == expected_policy and binding['source_policy_sha256'] == canonical_digest(expected_policy)
                and binding['raw_sha256'] == snapshot['sha256'] == canonical_digest(snapshot['raw']) == canonical_digest(posting['raw'])
                and binding['normalized_sha256'] == canonical_digest(projection)
                and binding['selected_requisition'] == projection['requisition']
                and quality['configured_source_id'] == source_id and quality['succeeded'] is True and quality['available'] is True
                and quality['source_endpoint'] == company_source['endpoint']
                and datetime.fromisoformat(parent['started_at']) <= datetime.fromisoformat(quality['observed_at'])
                    <= (datetime.fromisoformat(parent['completed_at']) if parent['state'] == 'COMPLETED'
                        else datetime.now(timezone.utc)))
            operation = _one(connection, m.operations, row['operation_id']) if row['operation_id'] else None
            if operation is not None:
                child_params = operation['params']
                _require(operation['kind'] == 'COMPANY_DISCOVER' and operation['entity_id'] == row['company_id']
                    and child_params.get('discovery_request_id') == row['id']
                    and type(child_params.get('discovery_request_revision')) is int
                    and child_params['discovery_request_revision'] == row['revision']
                    and child_params.get('profile_id') == profile_id and child_params.get('profile_version') == profile['version'])
                result['enrolled_sources'].extend(_enrolled_sources(connection, row, pending, operation))
            result['children'].append(dict(request_id=row['id'], revision=row['revision'], company_id=row['company_id'],
                company_source_id=row['company_source_id'], profile_id=profile_id, profile_version=profile['version'],
                source_id=source_id, parent_operation_id=parent_operation_id, parent_operation_attempt=attempt,
                trigger_job_id=row['trigger_job_id'], candidate_id=candidate['id'], posting_id=posting['id'],
                snapshot_id=snapshot['id'], snapshot_sha256=snapshot['sha256'], quality_observation_id=quality['id'],
                operation_id=operation['id'] if operation else None, state=operation['state'] if operation else row['state'],
                authorization=_code(row['authorization']), reason=_code(operation['error']) if operation else None,
                operation_attempt=operation['attempt_count'] if operation else 0,
                operation_sha256=canonical_digest(dict(operation)) if operation else None, pending_sha256=pending['sha256']))
        _require(len(result['enrolled_sources']) <= 2
            and len({item['source_id'] for item in result['enrolled_sources']}) == len(result['enrolled_sources']))
        result['enrolled_sources'].sort(key=lambda item: item['source_id'])
        result['lineage_sha256'] = canonical_digest([{key: value for key, value in row.items()
            if key not in {'state', 'authorization', 'reason', 'operation_attempt', 'operation_sha256'}} for row in result['children']])
        result['sha256'] = canonical_digest(result)
        return result


def create_collection_children_router(store):
    router = APIRouter()

    @router.get('/profiles/{profile_id}/sources/{source_id}/collections/{parent_operation_id}/children')
    def children(profile_id: str, source_id: str, parent_operation_id: str, attempt: int = Query(ge=1)):
        try:
            return collection_children(store, profile_id, source_id, parent_operation_id, attempt)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(409, 'COLLECTION_CHILD_LINEAGE_CHANGED') from None

    return router
