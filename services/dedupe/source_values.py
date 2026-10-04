"""Resolve already-attached posting values without promoting inferred authority."""
from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit

from sqlalchemy import func, select, update

from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.company_discovery.domains import VERIFIED_METHODS, is_aggregator_domain, normalize_domain
from services.company_discovery.quality import company_source_quality
from services.company_discovery.sources import _url
from services.dedupe.policy import SOURCE_AUTHORITY


BUNDLE_FIELDS = ('title', 'description', 'skills', 'location', 'remote_type', 'attributes')
LIFECYCLE_FIELDS = ('first_seen_at', 'last_seen_at', 'last_collected_at')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def lock_candidate_job(connection, values):
    upsert(connection, m.jobs, values)
    current = connection.execute(select(m.jobs).where(m.jobs.c.id == values['id']).with_for_update()).mappings().one()
    if current['company_id'] != values['company_id'] or current['requisition'] != values['requisition']:
        raise ValueError('SOURCE_VALUE_CANONICAL_IDENTITY_MISMATCH')
    return dict(current)


def record_candidate(connection, *, posting_id, company_source_id, observation_id, normalized):
    """Capture normalized values only alongside their exact committed source evidence."""
    posting = connection.execute(select(m.postings).where(m.postings.c.id == posting_id)).mappings().one()
    job = connection.execute(select(m.jobs).where(m.jobs.c.id == posting['job_id'])).mappings().one()
    source = connection.execute(select(m.company_sources).where(m.company_sources.c.id == company_source_id)).mappings().one()
    observation = connection.execute(select(m.source_quality_observations).where(
        m.source_quality_observations.c.id == observation_id)).mappings().one()
    values = deepcopy(normalized)
    if set(values) != set(BUNDLE_FIELDS) | {'requisition'} or not isinstance(values['attributes'], dict):
        raise ValueError('SOURCE_VALUE_BUNDLE_INVALID')
    attrs = values['attributes']
    if (source['company_id'] != job['company_id']
            or attrs.get('provider') != source['provider'] or attrs.get('tenant') != source['tenant']
            or _url(posting['source_url'], 'ENDPOINT') != source['endpoint']
            or _url(attrs.get('source_endpoint'), 'ENDPOINT') != source['endpoint']):
        raise ValueError('SOURCE_VALUE_IDENTITY_MISMATCH')
    if observation['source_endpoint'] != source['endpoint'] or not observation['succeeded'] or not (
        observation['company_source_id'] == source['id'] or
        (source['collector_source_id'] is not None and
         observation['configured_source_id'] == source['collector_source_id'])):
        raise ValueError('SOURCE_VALUE_OBSERVATION_MISMATCH')
    if attrs.get('source_id') not in {source['id'], source['collector_source_id']}:
        raise ValueError('SOURCE_VALUE_IDENTITY_MISMATCH')
    # Import timestamps belong to the canonical lifecycle, not the source's claims.
    for key in (*LIFECYCLE_FIELDS, 'source_resolution'):
        attrs.pop(key, None)
    application_url = posting['application_url'] or ''
    if application_url:
        _url(application_url, 'APPLICATION_URL', required=True)
    checksum = hashlib.sha256(_json(posting['raw']).encode()).hexdigest()
    snapshot_id = identity(posting_id, checksum)
    snapshot = connection.execute(select(m.posting_snapshots).where(
        m.posting_snapshots.c.id == snapshot_id, m.posting_snapshots.c.posting_id == posting_id)).mappings().one()
    if snapshot['sha256'] != checksum:
        raise ValueError('SOURCE_VALUE_SNAPSHOT_MISMATCH')
    candidate_id = identity('posting-value', posting_id, snapshot_id, company_source_id, observation_id, _json(values))
    upsert(connection, m.posting_value_candidates, dict(id=candidate_id, posting_id=posting_id,
        posting_snapshot_id=snapshot_id, company_source_id=company_source_id,
        quality_observation_id=observation_id, observed_at=observation['observed_at'],
        normalized=values, application_url=application_url))
    from services.discovery.lifecycle import observe_candidate
    observe_candidate(connection, candidate_id)
    return candidate_id


def _authority(connection, source):
    """Origin evidence is independent of quality and never read from raw job data."""
    parsed = urlsplit(source['endpoint'])
    collector_id = source['collector_source_id']
    if collector_id and source['provider'] == 'greenhouse':
        collector = connection.execute(select(m.job_sources).where(m.job_sources.c.id == collector_id)).mappings().one()
        tenant = connection.execute(select(m.company_ats_tenants).where(
            m.company_ats_tenants.c.id == source['ats_tenant_id'])).mappings().one_or_none()
        if (collector['provider'] == 'greenhouse' and collector['tenant'] == source['tenant']
                and parsed.scheme == 'https' and parsed.netloc == 'boards-api.greenhouse.io'
                and parsed.path == f"/v1/boards/{source['tenant']}/jobs" and not parsed.query
                and tenant and tenant['company_id'] == source['company_id']
                and tenant['provider'] == 'greenhouse' and tenant['tenant_identifier'] == source['tenant']
                and source['company_id'] == identity('greenhouse', source['tenant'])):
            return 'VERIFIED_ATS', ['Collected from the configured native Greenhouse API and matching board tenant.',
                                    'This verifies the ATS feed origin, not the employer domain or identity.']
    domain = normalize_domain(source['endpoint'])
    verified = connection.execute(select(m.company_domains).where(
        m.company_domains.c.company_id == source['company_id'], m.company_domains.c.domain == domain,
        m.company_domains.c.verified.is_(True))).mappings().all()
    if not is_aggregator_domain(domain) and any(row['verification_method'] in VERIFIED_METHODS and row['evidence'] for row in verified):
        return 'VERIFIED_EMPLOYER', ['Feed origin matches an independently verified domain for this company.']
    if is_aggregator_domain(domain):
        return 'AGGREGATOR', ['Source origin is a known aggregator domain.']
    return 'UNKNOWN', ['No qualifying independent source-authority evidence is stored.']


def _refresh_match(connection, job, profile):
    if profile is None:
        return
    usable = {fact['text'].strip().casefold() for fact in profile['facts'] if fact['kind'] == 'skill'
              and fact['verified'] and fact['user_approved'] and fact['source'].strip() and fact['confidence'] >= 80}
    mentions = {skill.casefold() for skill in job['skills']}
    score = round(100 * len(mentions & usable) / len(mentions), 2) if mentions else 0
    upsert(connection, m.matches, dict(id=identity(job['id'], profile['id'], profile['version']),
        job_id=job['id'], profile_id=profile['id'], profile_version=profile['version'], score=score,
        missing=sorted(mentions - usable), components=dict(method='description_skill_mentions',
            matched=sorted(mentions & usable), detected=sorted(mentions),
            limitation='Skill mentions are not verified mandatory requirements or a full qualification assessment.')),
        replace=True)


def resolve_job_values(connection, job_id, *, at, seen_at=None, profile=None):
    """Apply one whole candidate bundle, with source authority outranking quality."""
    job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == job_id).with_for_update()).mappings().one())
    from services.discovery.lifecycle import capture_revision, resolve_lifecycle
    ranked = select(m.posting_value_candidates, func.row_number().over(
        partition_by=m.posting_value_candidates.c.posting_id,
        order_by=(m.posting_value_candidates.c.observed_at.desc(), m.posting_value_candidates.c.id.desc())).label('rank'))
    ranked = ranked.join(m.postings, m.postings.c.id == m.posting_value_candidates.c.posting_id).where(m.postings.c.job_id == job_id).subquery()
    candidates = [dict(row) for row in connection.execute(select(ranked).where(ranked.c.rank == 1)).mappings()]
    if not candidates:
        return job
    capture_revision(connection, job, recorded_at=at,
                     observed_at=job['attributes'].get('last_seen_at'))
    for candidate in candidates:
        source = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == candidate['company_source_id'])).mappings().one()
        if source['company_id'] != job['company_id']:
            raise ValueError('SOURCE_VALUE_IDENTITY_MISMATCH')
        observation = connection.execute(select(m.source_quality_observations).where(
            m.source_quality_observations.c.id == candidate['quality_observation_id'])).mappings().one()
        original = candidate['normalized']['attributes']
        # A retargeted portal must not lend its new origin or collector identity
        # to a historical candidate. These fields were validated at capture.
        source = {**source, 'endpoint': _url(original['source_endpoint'], 'ENDPOINT'),
                  'provider': original['provider'], 'tenant': original['tenant'],
                  'collector_source_id': observation['configured_source_id']}
        authority, evidence = _authority(connection, source)
        quality = company_source_quality(connection, source, at=at)
        candidate.update(authority=authority, authority_evidence=evidence,
                         quality={'score': quality['score'], 'coverage': quality['coverage']},
                         effective_quality=None if quality['score'] is None else quality['score'] * quality['coverage'] / 100)
    winner = max(candidates, key=lambda row: (SOURCE_AUTHORITY[row['authority']],
        row['effective_quality'] if row['effective_quality'] is not None else -1,
        row['observed_at'], row['id']))
    attrs = deepcopy(winner['normalized']['attributes'])
    old_attrs = dict(job['attributes'])
    attrs.update({key: old_attrs.get(key) for key in LIFECYCLE_FIELDS})
    attrs['first_seen_at'] = old_attrs.get('first_seen_at') or at
    attrs['last_seen_at'] = seen_at or at
    attrs['last_collected_at'] = seen_at or at
    conflicts = [field for field in BUNDLE_FIELDS if field != 'attributes' and
                 len({_json(row['normalized'][field]) for row in candidates}) > 1]
    if len({_json({k:v for k,v in row['normalized']['attributes'].items() if k.startswith('salary_')}) for row in candidates}) > 1:
        conflicts.append('salary')
    # Only the canonical route created by these collectors may be updated here.
    route = connection.execute(select(m.routes).where(m.routes.c.id == job_id).with_for_update()).mappings().one()
    in_use = connection.execute(select(m.applications.c.id).where(m.applications.c.route_id == route['id']).limit(1)).first()
    if not in_use:
        in_use = connection.execute(select(m.application_route_choices.c.id).where(
            m.application_route_choices.c.route_id == route['id']).limit(1)).first()
    same_route_identity = (route['provider'] == winner['normalized']['attributes']['provider']
        and route['tenant'] == winner['normalized']['attributes']['tenant']
        and route['requisition'] == winner['normalized']['requisition'])
    if not in_use and same_route_identity:
        connection.execute(update(m.routes).where(m.routes.c.id == route['id']).values(url=winner['application_url']))
    mismatch = bool((in_use or not same_route_identity) and route['url'] != winner['application_url'])
    attrs['source_resolution'] = dict(
        selected_candidate_id=winner['id'], selected_posting_id=winner['posting_id'],
        authority=winner['authority'], authority_evidence=winner['authority_evidence'],
        quality=winner['quality'], effective_quality=winner['effective_quality'], evaluated_at=at,
        candidate_count=len(candidates), conflicts=conflicts,
        selected_application_url=winner['application_url'], route_review_required=mismatch,
        preserved_route_id=route['id'] if mismatch else None, preserved_route_url=route['url'] if mismatch else None,
        candidates=[dict(id=row['id'], posting_id=row['posting_id'], company_source_id=row['company_source_id'],
            authority=row['authority'], quality=row['quality'], observed_at=row['observed_at']) for row in sorted(candidates, key=lambda row:row['id'])])
    values = {key: deepcopy(winner['normalized'][key]) for key in BUNDLE_FIELDS if key != 'attributes'}
    values['attributes'] = attrs
    connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(**values))
    job.update(values)
    _refresh_match(connection, job, profile)
    capture_revision(connection, job, recorded_at=at, candidate_id=winner['id'], observed_at=winner['observed_at'])
    resolve_lifecycle(connection, job_id)
    return job


def require_route_review(connection, job_id, route_mode, route_id):
    attrs = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one()
    resolution = (attrs or {}).get('source_resolution') or {}
    if resolution.get('route_review_required') and not (
        str(route_mode).upper() == 'THIS_ROUTE' and route_id == resolution.get('preserved_route_id')):
        raise ValueError('APPLICATION_SOURCE_ROUTE_REVIEW_REQUIRED')
