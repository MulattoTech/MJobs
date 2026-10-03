"""Durable, source-attributed collection evidence; never inferred historical PASS."""
from datetime import datetime, timedelta, timezone
import re
from uuid import uuid4

from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.quality_score import quality_score


def posting_metrics(job, application_url):
    """Completeness is field presence, not proof that its content is accurate."""
    location = str(job.get('location') or '').strip().casefold()
    fields = (bool(str(job.get('title') or '').strip()), bool(str(job.get('description') or '').strip()),
              location not in {'', 'unknown', 'not specified', 'n/a'}, bool(application_url))
    attributes = job.get('attributes') or {}
    salary = any(type(attributes.get(key)) in (int, float) for key in ('salary_min', 'salary_max'))
    return sum(fields) / len(fields), salary


def record_observation(connection, *, configured_source_id=None, company_source_id=None,
                       observed_at=None, succeeded, available=None, enumerated=None, accepted=None,
                       completeness_sum=None, salary_known=None, links_checked=None,
                       links_accurate=None, error_code=None, source_endpoint=None):
    if bool(configured_source_id) == bool(company_source_id):
        raise ValueError('QUALITY_EXACTLY_ONE_SOURCE_REQUIRED')
    if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,100}', error_code)):
        raise ValueError('QUALITY_ERROR_CODE_INVALID')
    timestamp = observed_at or datetime.now(timezone.utc)
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError('QUALITY_OBSERVATION_TIME_INVALID')
    timestamp = timestamp.astimezone(timezone.utc).isoformat()
    from services.company_discovery.sources import _url
    if source_endpoint is None:
        if company_source_id:
            source_endpoint = connection.execute(select(m.company_sources.c.endpoint).where(
                m.company_sources.c.id == company_source_id)).scalar_one()
        else:
            collector = connection.execute(select(m.job_sources).where(m.job_sources.c.id == configured_source_id)).mappings().one()
            if collector['provider'] == 'greenhouse':
                source_endpoint = f"https://boards-api.greenhouse.io/v1/boards/{collector['tenant']}/jobs"
    source_endpoint = _url(source_endpoint, 'ENDPOINT')
    row = dict(id=uuid4().hex, configured_source_id=configured_source_id, company_source_id=company_source_id,
               source_endpoint=source_endpoint,
               observed_at=timestamp, succeeded=succeeded, available=available, enumerated=enumerated,
               accepted=accepted, completeness_sum=completeness_sum, salary_known=salary_known,
               links_checked=links_checked, links_accurate=links_accurate, error_code=error_code)
    quality_score([row], at=timestamp)  # Reject invalid evidence before committing it.
    connection.execute(m.source_quality_observations.insert().values(**row))
    return row['id']


def source_quality(connection, *, configured_source_id=None, company_source_id=None, at=None, endpoint=None):
    """Bounded projection. Shared collector statistics retain their board-wide scope."""
    if bool(configured_source_id) == bool(company_source_id):
        raise ValueError('QUALITY_EXACTLY_ONE_SOURCE_REQUIRED')
    reference = at or datetime.now(timezone.utc)
    if isinstance(reference, str):
        reference = datetime.fromisoformat(reference.replace('Z', '+00:00'))
    if reference.tzinfo is None:
        raise ValueError('QUALITY_REFERENCE_TIME_INVALID')
    reference = reference.astimezone(timezone.utc)
    column = m.source_quality_observations.c.configured_source_id if configured_source_id else m.source_quality_observations.c.company_source_id
    source_id = configured_source_id or company_source_id
    if endpoint is None and configured_source_id:
        collector = connection.execute(select(m.job_sources).where(m.job_sources.c.id == configured_source_id)).mappings().one()
        if collector['provider'] == 'greenhouse':
            endpoint = f"https://boards-api.greenhouse.io/v1/boards/{collector['tenant']}/jobs"
    query = select(m.source_quality_observations).where(column == source_id,
        m.source_quality_observations.c.observed_at >= (reference - timedelta(days=30)).isoformat(),
        m.source_quality_observations.c.observed_at <= reference.isoformat()).order_by(
        m.source_quality_observations.c.observed_at.desc(), m.source_quality_observations.c.id.desc()).limit(200)
    if endpoint is not None:
        from services.company_discovery.sources import _url
        query = query.where(m.source_quality_observations.c.source_endpoint == _url(endpoint, 'ENDPOINT'))
    observations = [dict(row) for row in connection.execute(query).mappings()]
    return {**quality_score(observations, at=reference), 'source_id': source_id,
            'source_endpoint': endpoint,
            'scope': 'CONFIGURED_SOURCE' if configured_source_id else 'COMPANY_SOURCE'}


def company_source_quality(connection, source, *, at=None):
    if source['collector_source_id']:
        return source_quality(connection, configured_source_id=source['collector_source_id'], at=at, endpoint=source['endpoint'])
    return source_quality(connection, company_source_id=source['id'], at=at, endpoint=source['endpoint'])


def record_known_feed_failure(store, *, company_id, career_url, endpoint, strategy, available, error_code):
    """Attribute a failed reacquisition only to an already enrolled exact portal."""
    from services.company_discovery.sources import _url
    provider = 'rss' if strategy == 'RSS' else 'json_feed'
    with store.engine.begin() as c:
        sources = c.execute(select(m.company_sources.c.id).where(
            m.company_sources.c.company_id == company_id,
            m.company_sources.c.provider == provider,
            m.company_sources.c.tenant == company_id,
            m.company_sources.c.career_url == _url(career_url, 'CAREER_URL'),
            m.company_sources.c.endpoint == _url(endpoint, 'ENDPOINT'))).scalars().all()
        for source_id in sources:
            record_observation(c, company_source_id=source_id, succeeded=False,
                               available=available, error_code=error_code, source_endpoint=endpoint)
