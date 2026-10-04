"""Append-only evidence and conservative canonical lifecycle resolution."""
from copy import deepcopy
from urllib.parse import urlsplit
import hashlib
import json

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import identity, upsert


STATES = frozenset({'ACTIVE', 'POSSIBLY_ACTIVE', 'REMOVED', 'EXPIRED', 'FILLED', 'UNKNOWN', 'CLOSED'})
CLOSED_STATES = frozenset({'REMOVED', 'EXPIRED', 'FILLED', 'CLOSED'})


def status_claim(value, *, default='POSSIBLY_ACTIVE'):
    """Only exact structured states are recognized; prose never proves closure."""
    if value is None:
        return default
    return value.strip().upper() if isinstance(value, str) and value.strip().upper() in STATES else 'UNKNOWN'


def observe_candidate(connection, candidate_id):
    from services.dedupe.source_values import _authority
    candidate = connection.execute(select(m.posting_value_candidates).where(
        m.posting_value_candidates.c.id == candidate_id)).mappings().one()
    posting = connection.execute(select(m.postings).where(m.postings.c.id == candidate['posting_id'])).mappings().one()
    source = connection.execute(select(m.company_sources).where(m.company_sources.c.id == candidate['company_source_id'])).mappings().one()
    authority, proof = _authority(connection, source)
    claim = candidate['normalized']['attributes'].get('lifecycle_claim', 'POSSIBLY_ACTIVE')
    upsert(connection, m.job_lifecycle_observations, dict(
        id=identity('lifecycle', candidate_id), job_id=posting['job_id'], posting_id=posting['id'],
        candidate_id=candidate_id, quality_observation_id=candidate['quality_observation_id'],
        observed_at=candidate['observed_at'], status=status_claim(claim), authority=authority,
        evidence=dict(kind='STRUCTURED_STATUS' if claim != 'POSSIBLY_ACTIVE' else 'LISTED',
                      source_endpoint=source['endpoint'], authority=proof)))


def observe_fixture_posting(connection, posting_id):
    """The fictional local flywheel has no production source-value candidates."""
    posting = connection.execute(select(m.postings).where(m.postings.c.id == posting_id)).mappings().one()
    job = connection.execute(select(m.jobs).where(m.jobs.c.id == posting['job_id'])).mappings().one()
    parsed = urlsplit(posting['source'])
    aggregator = posting['source'] == 'mock-board'
    if not job['attributes'].get('fictional') or not (aggregator or
            parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', 'mock'}):
        raise ValueError('LIFECYCLE_FIXTURE_ORIGIN_REQUIRED')
    authority = 'AGGREGATOR' if aggregator else 'DIRECT_EMPLOYER'
    checksum = _hash(posting['raw'])
    status = status_claim(posting['raw'].get('status'), default='POSSIBLY_ACTIVE' if aggregator else 'ACTIVE')
    upsert(connection, m.job_lifecycle_observations, dict(
        id=identity('fixture-lifecycle', posting_id, checksum, posting['fetched_at']),
        job_id=posting['job_id'], posting_id=posting_id, candidate_id=None, quality_observation_id=None,
        observed_at=posting['fetched_at'], status=status, authority=authority,
        evidence=dict(kind='FICTIONAL_LOCAL_SOURCE', source_endpoint=posting['source'],
            snapshot_id=identity(posting_id, checksum),
            authority=['Fictional source constrained by the local-only MockClient origin boundary.'])))


def resolve_lifecycle(connection, job_id):
    from services.dedupe.policy import SOURCE_AUTHORITY
    job = connection.execute(select(m.jobs).where(m.jobs.c.id == job_id).with_for_update()).mappings().one()
    rows = connection.execute(select(m.job_lifecycle_observations).where(
        m.job_lifecycle_observations.c.job_id == job_id).order_by(
        m.job_lifecycle_observations.c.observed_at, m.job_lifecycle_observations.c.id)).mappings().all()
    latest = {}
    for row in rows:
        previous = latest.get(row['posting_id'])
        if previous and previous['status'] in CLOSED_STATES and row['status'] in {'UNKNOWN', 'POSSIBLY_ACTIVE'}:
            continue
        latest[row['posting_id']] = row
    if not latest:
        return
    winner = max(latest.values(), key=lambda row: (SOURCE_AUTHORITY[row['authority']], row['observed_at'], row['id']))
    attrs = deepcopy(job['attributes'])
    attrs.update(status=winner['status'], lifecycle=dict(observation_id=winner['id'],
        authority=winner['authority'], observed_at=winner['observed_at'], evidence=winner['evidence']))
    connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(attributes=attrs))


def reconcile_inventory(connection, *, endpoint, posting_ids, observation_id):
    """Caller must establish successful, unfiltered, fully parsed inventory."""
    observation = connection.execute(select(m.source_quality_observations).where(
        m.source_quality_observations.c.id == observation_id)).mappings().one()
    if not (observation['succeeded'] and observation['available'] and observation['source_endpoint'] == endpoint
            and observation['enumerated'] == observation['accepted']):
        raise ValueError('LIFECYCLE_INCOMPLETE_INVENTORY')
    rows = connection.execute(select(m.job_lifecycle_observations).join(
        m.postings, m.postings.c.id == m.job_lifecycle_observations.c.posting_id).where(
        m.postings.c.source_url == endpoint).order_by(
        m.job_lifecycle_observations.c.observed_at, m.job_lifecycle_observations.c.id)).mappings().all()
    latest = {row['posting_id']: row for row in rows}
    for row in sorted(latest.values(), key=lambda row: (row['job_id'], row['posting_id'])):
        if row['posting_id'] in posting_ids or row['status'] in CLOSED_STATES or row['observed_at'] >= observation['observed_at']:
            continue
        # A retargeted source/collector cannot establish absence in its predecessor.
        previous = connection.execute(select(m.source_quality_observations).where(
            m.source_quality_observations.c.id == row['quality_observation_id'])).mappings().one()
        if (previous['configured_source_id'], previous['company_source_id']) != (
                observation['configured_source_id'], observation['company_source_id']):
            continue
        connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == row['job_id']).with_for_update()).one()
        upsert(connection, m.job_lifecycle_observations, dict(id=identity('missing', row['posting_id'], observation_id),
            job_id=row['job_id'], posting_id=row['posting_id'], candidate_id=row['candidate_id'],
            quality_observation_id=observation_id, observed_at=observation['observed_at'], status='REMOVED',
            authority=row['authority'], evidence=dict(kind='ABSENT_FROM_COMPLETE_INVENTORY',
                source_endpoint=endpoint, authority=row['evidence']['authority'])))
        resolve_lifecycle(connection, row['job_id'])


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def capture_revision(connection, job, *, recorded_at, candidate_id=None, observed_at=None):
    """Serialize on canonical row; no updates/deletes and no A -> B -> A collapse."""
    connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == job['id']).with_for_update()).one()
    attrs = job['attributes']
    description = job['description']
    location = job['location'] if job['location'] and job['location'].casefold() != 'unknown' else None
    salary = dict(min=attrs.get('salary_min'), max=attrs.get('salary_max'),
                  currency=attrs.get('salary_currency'), period=attrs.get('salary_period', 'UNKNOWN'))
    remote = dict(type=job['remote_type'], regions=sorted(set(attrs.get('remote_regions') or [])))
    material_hash = _hash(dict(description=' '.join(description.split()), salary=salary,
        location=' '.join(location.split()) if location else None, remote_policy=remote))
    previous = connection.execute(select(m.job_description_revisions).where(
        m.job_description_revisions.c.job_id == job['id']).order_by(
        m.job_description_revisions.c.sequence.desc()).limit(1)).mappings().one_or_none()
    if previous and previous['material_hash'] == material_hash:
        return previous['id']
    sequence = previous['sequence'] + 1 if previous else 1
    revision_id = identity('job-description', job['id'], sequence)
    connection.execute(m.job_description_revisions.insert().values(id=revision_id, job_id=job['id'],
        sequence=sequence, candidate_id=candidate_id, description_hash=hashlib.sha256(description.encode()).hexdigest(),
        material_hash=material_hash, description=description, salary=salary, location=location,
        remote_policy=remote, observed_at=observed_at, recorded_at=recorded_at))
    return revision_id
