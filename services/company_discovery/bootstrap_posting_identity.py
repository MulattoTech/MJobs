"""Attach only a new, DB-authorized bootstrap native posting to its trigger.

Existing native jobs, postings and routes remain sticky; this never merges jobs.
Callers hold normal ingestion/source locks and still run the final import guard.
"""
from datetime import datetime, timezone

from sqlalchemy import or_, select

from packages.persistence import models as m
from services.company_discovery.public_client import PublicFetchError


def _require(condition):
    if not condition:
        raise PublicFetchError('DISCOVERY_BOOTSTRAP_IDENTITY_CHANGED')


def _one(connection, table, identifier, *, lock=False):
    query = select(table).where(table.c.id == identifier)
    row = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    _require(row is not None)
    return row


def authorized_native_context(connection, store, operation, profile, source, bound):
    """Rebuild live authority; operation arguments alone never grant identity."""
    from services.company_discovery.requests import authorize_discovery_request

    current = _one(connection, m.operations, operation['id'], lock=True)
    lease = datetime.fromisoformat(current['lease_expires_at']) if current['lease_expires_at'] else None
    _require(current['kind'] == 'COMPANY_DISCOVER' and current['state'] == 'RUNNING'
        and current['attempt_count'] == operation['attempt_count'] and current['params'] == operation['params']
        and not current['cancel_requested'] and lease is not None and lease.tzinfo is not None
        and lease > datetime.now(timezone.utc))
    authorization = authorize_discovery_request(store, dict(current), connection=connection)
    _require(authorization['authorization'] == 'AGGREGATOR_BOOTSTRAP_VERIFIED'
        and authorization.get('aggregator_bootstrap_domain') is not None)
    pending = authorization['aggregator_bootstrap_pending']
    binding = pending['binding']
    receipt = authorization['aggregator_bootstrap_receipt']
    native = receipt['native']
    live_source = _one(connection, m.job_sources, source['id'])
    _require(bound is not None)
    live_bound = _one(connection, m.company_sources, bound['id'])
    _require(all(source[key] == live_source[key] for key in (
        'provider', 'tenant', 'enabled', 'strategy', 'capabilities', 'request_timeout', 'rate_limit', 'concurrency'))
        and all(bound[key] == live_bound[key] for key in (
            'company_id', 'collector_source_id', 'provider', 'tenant', 'enabled', 'endpoint', 'career_url')))
    _require(bound['career_url'] == receipt['employer_url']
        and bound['company_id'] == current['entity_id'] == authorization['company_id']
        and binding['company_id'] == bound['company_id']
        and binding['profile_id'] == profile['id'] and binding['profile_version'] == profile['version']
        and _one(connection, m.profiles, profile['id'])['version'] == profile['version']
        and source['enabled'] and bound['enabled'] and source['provider'] == bound['provider'] == native['provider'] == 'greenhouse'
        and source['tenant'] == bound['tenant'] == native['tenant']
        and bound['collector_source_id'] == source['id']
        and bound['endpoint'] == 'https://boards-api.greenhouse.io/v1/boards/' + source['tenant'] + '/jobs')
    return current, authorization, pending


def receipt_native_job_id(connection, operation):
    """Negative prefilter only; a matching ID still needs full live authority."""
    return connection.execute(select(
        m.company_discovery_requests.c.domain_evidence[1]['receipt']['native']['job_id'].as_string()
    ).where(m.company_discovery_requests.c.id == operation['params'].get('discovery_request_id'))).scalar_one_or_none()


def bootstrap_posting_job(connection, store, *, operation, profile, source, bound,
                          raw, requisition, proposed_id, posting_id, source_job_id, batch_identities):
    """Fallback only after ordinary canonical ingestion found no sticky target."""
    _, authorization, pending = authorized_native_context(connection, store, operation, profile, source, bound)
    native = authorization['aggregator_bootstrap_receipt']['native']
    # Other openings on the same board are ordinary independent jobs. Titles,
    # scores, and similar URLs never extend this one corroborated native ID.
    if (type(raw.get('id')) is not int or str(raw['id']) != native['job_id']
            or requisition != native['job_id'] or source_job_id != native['job_id']):
        return None
    binding = pending['binding']
    target = _one(connection, m.jobs, binding['trigger_job_id'], lock=True)
    candidate = _one(connection, m.posting_value_candidates, binding['candidate_id'])
    trigger_posting = _one(connection, m.postings, candidate['posting_id'])
    _require(target['company_id'] == bound['company_id']
        and trigger_posting['job_id'] == target['id']
        and candidate['company_source_id'] == binding['company_source_id']
        and candidate['posting_snapshot_id'] == binding['posting_snapshot_id']
        and candidate['quality_observation_id'] == binding['quality_observation_id'])
    # Do not override a sticky identity the normal selector could not resolve
    # (including alternate posting IDs and ambiguous or foreign native routes).
    _require(connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == proposed_id)).first() is None
        and connection.execute(select(m.postings.c.id).where(or_(m.postings.c.id == posting_id,
            (m.postings.c.source == bound['endpoint']) & (m.postings.c.source_job_id == source_job_id)))).first() is None
        and connection.execute(select(m.routes.c.id).where(or_(m.routes.c.id == proposed_id,
            (m.routes.c.provider == source['provider']) & (m.routes.c.tenant == source['tenant'])
                & (m.routes.c.requisition == requisition)))).first() is None)
    identities = [(row['provider'], row['tenant'], row['requisition']) for row in connection.execute(
        select(m.routes).where(m.routes.c.job_id == target['id'])).mappings()]
    identities.extend(batch_identities.get(target['id'], []))
    values = connection.execute(select(m.posting_value_candidates.c.normalized).join(m.postings,
        m.postings.c.id == m.posting_value_candidates.c.posting_id).where(m.postings.c.job_id == target['id'])).scalars()
    identities.extend((value['attributes'].get('provider'), value['attributes'].get('tenant'),
        value['requisition']) for value in values)
    _require(not any(provider == source['provider'] and tenant == source['tenant'] and other != requisition
        for provider, tenant, other in identities))
    return dict(target)
