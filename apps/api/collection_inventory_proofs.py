"""Read-only exact-child native inventory receipts; no acceptance inference."""
from sqlalchemy import select

from packages.persistence import models as m
from apps.api.collection_children import _collection_children_on, _one, _require
from apps.api.collection_domain_proofs import _domain_proof_on
from services.company_discovery.aggregator_bootstrap import canonical_digest
from services.discovery.source_collection_receipts import ACTION, validate_native_inventory


def collection_inventory_proofs(store, profile_id, source_id, parent_operation_id, attempt):
    with store.read_snapshot() as connection:
        collection = _collection_children_on(connection, profile_id, source_id, parent_operation_id, attempt)
        result = {key: collection[key] for key in ('schema_version', 'profile_id', 'profile_version', 'source_id',
            'parent_operation_id', 'parent_operation_attempt', 'parent_request_id', 'complete', 'limit')}
        result.update(collection_lineage_sha256=collection['lineage_sha256'],
            collection_readback_sha256=collection['sha256'], observations=[])
        for child in collection['children']:
            observation = dict(child=child, status='UNAVAILABLE', reason='NATIVE_INVENTORY_NOT_PERSISTED', proof=None)
            result['observations'].append(observation)
            if not child['operation_id'] or child['state'] != 'COMPLETED':
                observation['reason'] = 'CHILD_NOT_COMPLETED'
                continue
            # Validate current domain/bootstrap authority, including mutable policy.
            domain, reason = _domain_proof_on(connection, child)
            if domain is None:
                observation['reason'] = reason
                continue
            request = _one(connection, m.company_discovery_requests, child['request_id'])
            operation = _one(connection, m.operations, child['operation_id'])
            enrolled = [row for row in collection['enrolled_sources'] if row['request_id'] == child['request_id']]
            if not enrolled:
                observation['reason'] = 'NATIVE_SOURCE_NOT_ENROLLED'
                continue
            _require(len(enrolled) == 1)
            native = enrolled[0]
            source = _one(connection, m.job_sources, native['source_id'])
            bound = _one(connection, m.company_sources, native['company_source_id'])
            # A recovered operation can retain committed receipts from earlier
            # attempts. Select this exact attempt; reject duplicates within it.
            events = connection.execute(select(m.audit).where(m.audit.c.action == ACTION,
                m.audit.c.payload['native_inventory']['child_operation_id'].as_string() == operation['id'],
                m.audit.c.payload['native_inventory']['child_operation_attempt'].as_integer() == operation['attempt_count']
            ).order_by(m.audit.c.id).limit(2)).mappings().all()
            _require(len(events) <= 1)
            if not events:
                continue
            event = events[0]
            _require(event['entity_id'] == source['id'])
            receipt = event['payload']['native_inventory']
            validate_native_inventory(connection, receipt, child, request, operation, source, bound)
            _require(event['payload']['provider'] == receipt['provider']
                and type(event['payload']['jobs_seen']) is int
                and event['payload']['jobs_seen'] == receipt['accepted_count']
                and type(event['payload']['rejected']) is int
                and event['payload']['rejected'] == receipt['rejected_count'])
            proof = {**receipt, 'receipt_sha256': receipt['sha256'],
                'audit_id': event['id'], 'audit_sha256': canonical_digest(dict(event))}
            proof.pop('sha256')
            proof['sha256'] = canonical_digest(proof)
            observation.update(status=receipt['status'], reason=receipt['reason'], proof=proof)
        result['sha256'] = canonical_digest(result)
        return result
