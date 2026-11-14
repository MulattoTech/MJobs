"""Bounded durable receipts for a child worker's authorized native import.

These describe a committed provider batch, not public-network or acceptance proof.
Legacy/ordinary collectors do not emit this optional receipt.
"""
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.aggregator_bootstrap import canonical_digest, source_policy_projection

MAX_ENTRIES = 10_000
# At most two children: keep their combined proof comfortably below 8 MiB.
MAX_BYTES = 2 * 1024 * 1024
ACTION = 'SOURCE_COLLECTION_COMPLETE'
CAPACITY_REASON = 'INVENTORY_RECEIPT_CAPACITY_EXCEEDED'


def _require(condition):
    if not condition:
        raise ValueError('COLLECTION_INVENTORY_PROOF_CHANGED')


def _one(connection, table, identifier):
    row = connection.execute(select(table).where(table.c.id == identifier)).mappings().one_or_none()
    _require(row is not None)
    return row


def raw_digest(raw):
    # Matches immutable posting_snapshots, including non-ASCII provider values.
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def source_binding(source, bound):
    return dict(company_source_id=bound['id'], company_id=bound['company_id'],
        collector_source_id=source['id'], provider=source['provider'], tenant=source['tenant'],
        endpoint=bound['endpoint'], source_policy_sha256=canonical_digest(source_policy_projection(source)))


def _manifest_row(connection, candidate, observation_id, binding):
    posting = _one(connection, m.postings, candidate['posting_id'])
    snapshot = _one(connection, m.posting_snapshots, candidate['posting_snapshot_id'])
    job = _one(connection, m.jobs, posting['job_id'])
    _require(candidate['quality_observation_id'] == observation_id
        and candidate['company_source_id'] == binding['company_source_id']
        and snapshot['posting_id'] == posting['id']
        and snapshot['sha256'] == raw_digest(snapshot['raw'])
        and job['company_id'] == binding['company_id']
        and posting['source'] == binding['endpoint'])
    return dict(canonical_job_id=job['id'], posting_id=posting['id'], source_job_id=posting['source_job_id'],
        snapshot_id=snapshot['id'], snapshot_sha256=snapshot['sha256'], candidate_id=candidate['id'])


def bounded_receipt(binding, manifest):
    """Never substitute a truncated positive manifest for a capacity refusal."""
    result = {**binding, 'status': 'AVAILABLE', 'reason': None, 'manifest': manifest,
        'manifest_sha256': canonical_digest(manifest)}
    result['duplicate_count'] = result['accepted_count'] - len(manifest)
    _require(result['duplicate_count'] >= 0)
    result['inventory_complete'] = (result['provider_complete_inventory']
        and result['rejected_count'] == 0 and result['duplicate_count'] == 0)
    result['sha256'] = canonical_digest(result)
    if len(manifest) > MAX_ENTRIES or len(json.dumps(result, sort_keys=True).encode()) > MAX_BYTES:
        result.update(status='UNAVAILABLE', reason=CAPACITY_REASON, manifest=None,
            manifest_sha256=None, duplicate_count=None, inventory_complete=False)
    result.pop('sha256', None)
    result['sha256'] = canonical_digest(result)
    return result


def build_native_inventory(connection, store, operation, profile, source, bound, *,
                           observation_id, started_at, provider_complete, enumerated, accepted, rejected):
    """Called inside the existing import transaction after the normal guards."""
    from services.company_discovery.requests import authorize_discovery_request

    current = connection.execute(select(m.operations).where(
        m.operations.c.id == operation['id']).with_for_update()).mappings().one()
    _require(current['kind'] == 'COMPANY_DISCOVER' and current['state'] == 'RUNNING'
        and current['attempt_count'] == operation['attempt_count'] and current['params'] == operation['params']
        and not current['cancel_requested'] and current['lease_expires_at'] is not None
        and datetime.fromisoformat(current['lease_expires_at']) > datetime.now(timezone.utc))
    authorization = authorize_discovery_request(store, dict(current), connection=connection)
    _require(authorization['authorization'] == 'AGGREGATOR_BOOTSTRAP_VERIFIED')
    pending = authorization['aggregator_bootstrap_pending']
    parent = pending['binding']
    _require(bound is not None and bound['company_id'] == current['entity_id'] == authorization['company_id']
        and parent['profile_id'] == profile['id'] and parent['profile_version'] == profile['version']
        and parent['company_id'] == bound['company_id'] and source['enabled'] and bound['enabled']
        and source['provider'] == bound['provider'] == 'greenhouse'
        and source['tenant'] == bound['tenant'] and bound['collector_source_id'] == source['id'])
    binding = dict(schema_version=1, **source_binding(source, bound),
        child_operation_id=current['id'], child_operation_attempt=current['attempt_count'],
        operation_params_sha256=canonical_digest(current['params']),
        discovery_request_id=authorization['request_id'], request_revision=current['params']['discovery_request_revision'],
        parent_operation_id=parent['parent_operation_id'], parent_operation_attempt=parent['parent_operation_attempt'],
        parent_request_id=_one(connection, m.operations, parent['parent_operation_id'])['params']['request_id'],
        profile_id=profile['id'], profile_version=profile['version'], pending_sha256=pending['sha256'],
        bootstrap_receipt_sha256=authorization['aggregator_bootstrap_receipt']['sha256'],
        collection_started_at=started_at, quality_observation_id=observation_id,
        provider_complete_inventory=provider_complete, enumerated_count=enumerated,
        accepted_count=accepted, rejected_count=rejected)
    _require(type(provider_complete) is bool and enumerated == accepted + rejected)
    candidates = connection.execute(select(m.posting_value_candidates).where(
        m.posting_value_candidates.c.quality_observation_id == observation_id).order_by(
        m.posting_value_candidates.c.id).limit(MAX_ENTRIES + 1)).mappings().all()
    if len(candidates) > MAX_ENTRIES:
        # Avoid loading/serializing an unbounded manifest; ordinary import survives.
        result = {**binding, 'status': 'UNAVAILABLE', 'reason': CAPACITY_REASON,
            'manifest': None, 'manifest_sha256': None, 'duplicate_count': None, 'inventory_complete': False}
        result['sha256'] = canonical_digest(result)
        return result
    manifest = [_manifest_row(connection, candidate, observation_id, binding) for candidate in candidates]
    manifest.sort(key=lambda row: row['posting_id'])
    return bounded_receipt(binding, manifest)


def validate_native_inventory(connection, receipt, child, request, operation, source, bound):
    """Rebuild historical manifest identity under the caller's read snapshot."""
    _require(isinstance(receipt, dict) and receipt.get('schema_version') == 1
        and receipt.get('sha256') == canonical_digest({k: v for k, v in receipt.items() if k != 'sha256'}))
    _require(operation['state'] == 'COMPLETED' and operation['error'] is None
        and not operation['cancel_requested'])
    pending = request['domain_evidence'][0]
    expected = dict(**source_binding(source, bound), child_operation_id=operation['id'],
        child_operation_attempt=operation['attempt_count'], operation_params_sha256=canonical_digest(operation['params']),
        discovery_request_id=request['id'], request_revision=request['revision'],
        profile_id=child['profile_id'], profile_version=child['profile_version'],
        parent_operation_id=child['parent_operation_id'], parent_operation_attempt=child['parent_operation_attempt'],
        parent_request_id=_one(connection, m.operations, child['parent_operation_id'])['params']['request_id'],
        pending_sha256=pending['sha256'], bootstrap_receipt_sha256=request['domain_evidence'][1]['receipt']['sha256'])
    _require(set(receipt) == set(expected) | {'schema_version', 'collection_started_at',
        'quality_observation_id', 'provider_complete_inventory', 'enumerated_count', 'accepted_count',
        'rejected_count', 'status', 'reason', 'manifest', 'manifest_sha256', 'duplicate_count',
        'inventory_complete', 'sha256'} and type(receipt['schema_version']) is int)
    _require(all(type(receipt.get(k)) is type(v) and receipt[k] == v for k, v in expected.items()))
    for key in ('enumerated_count', 'accepted_count', 'rejected_count'):
        _require(type(receipt.get(key)) is int and receipt[key] >= 0)
    _require(type(receipt.get('provider_complete_inventory')) is bool
        and receipt['enumerated_count'] == receipt['accepted_count'] + receipt['rejected_count'])
    observation = _one(connection, m.source_quality_observations, receipt['quality_observation_id'])
    _require(observation['configured_source_id'] == source['id'] and observation['source_endpoint'] == bound['endpoint']
        and observation['succeeded'] is True and observation['available'] is True
        and observation['enumerated'] == receipt['enumerated_count'] and observation['accepted'] == receipt['accepted_count']
        and datetime.fromisoformat(operation['started_at']) <= datetime.fromisoformat(receipt['collection_started_at'])
            <= datetime.fromisoformat(observation['observed_at']) <= datetime.fromisoformat(operation['completed_at']))
    if receipt['status'] == 'UNAVAILABLE':
        _require(receipt['reason'] == CAPACITY_REASON and receipt['manifest'] is None
            and receipt['manifest_sha256'] is None and receipt['duplicate_count'] is None
            and receipt['inventory_complete'] is False)
        return
    manifest = receipt['manifest']
    _require(receipt['status'] == 'AVAILABLE' and receipt['reason'] is None and isinstance(manifest, list)
        and len(manifest) <= MAX_ENTRIES and len(json.dumps(receipt, sort_keys=True).encode()) <= MAX_BYTES
        and receipt['manifest_sha256'] == canonical_digest(manifest))
    candidates = connection.execute(select(m.posting_value_candidates).where(
        m.posting_value_candidates.c.quality_observation_id == observation['id']).order_by(
        m.posting_value_candidates.c.id).limit(MAX_ENTRIES + 1)).mappings().all()
    actual = sorted((_manifest_row(connection, c, observation['id'], receipt) for c in candidates), key=lambda row: row['posting_id'])
    _require(manifest == actual and len({row['posting_id'] for row in manifest}) == len(manifest)
        and type(receipt['duplicate_count']) is int and receipt['duplicate_count'] == receipt['accepted_count'] - len(manifest)
        and receipt['duplicate_count'] >= 0 and type(receipt['inventory_complete']) is bool
        and receipt['inventory_complete'] == (receipt['provider_complete_inventory']
            and receipt['rejected_count'] == 0 and receipt['duplicate_count'] == 0))
