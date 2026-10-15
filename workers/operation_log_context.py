"""Read-only, bounded correlations for already-persisted operation outcomes.

Params are lookup hints, never log fields. No provider/service calls, writes,
locks, or arbitrary first-row selection belong in this projection.
"""
from sqlalchemy import select

from packages.persistence import models as m
from workers.structured_logging import safe_operation_context


def _one(connection, statement):
    # Ambiguous relationships are deliberately absent, even if ordering is stable.
    rows = connection.execute(statement.limit(2)).mappings().all()
    return dict(rows[0]) if len(rows) == 1 else None


def _profile_exists(connection, profile_id):
    return isinstance(profile_id, str) and connection.execute(select(m.profiles.c.id).where(
        m.profiles.c.id == profile_id)).scalar_one_or_none() is not None


def _source_context(row):
    return {'source_id': row['id'], 'source': row['provider'],
            'ats_provider': row['provider']}


def _posting_context(connection, job_id, route):
    query = select(m.postings.c.id, m.postings.c.source, m.postings.c.source_url).where(m.postings.c.job_id == job_id)
    if route is not None:
        query = query.where(m.postings.c.application_url == route['url'])
    posting = _one(connection, query)
    if posting is None:
        return {}
    context = {'posting_id': posting['id'], 'source': posting['source']}
    # Prefer immutable posting lineage over mutable canonical-job attributes.
    candidates = m.posting_value_candidates.join(
        m.source_quality_observations,
        m.posting_value_candidates.c.quality_observation_id == m.source_quality_observations.c.id,
    )
    filters = (
        m.posting_value_candidates.c.posting_id == posting['id'],
        m.source_quality_observations.c.source_endpoint == posting['source_url'],
    )
    configured = select(m.job_sources.c.id, m.job_sources.c.provider).select_from(
        candidates.join(m.job_sources,
                        m.source_quality_observations.c.configured_source_id == m.job_sources.c.id)
    ).where(*filters)
    company = select(m.company_sources.c.id, m.company_sources.c.provider).select_from(
        candidates.join(m.company_sources,
                        m.source_quality_observations.c.company_source_id == m.company_sources.c.id)
    ).where(*filters)
    source = _one(connection, configured.union(company))
    if source:
        context.update({'source_id': source['id'], 'source': source['provider']})
    return context


def _job_context(connection, job, application=None, route_id=None):
    context = {'job_id': job['id'], 'company_id': job['company_id']}
    if application:
        context['application_id'] = application['id']
        route_id = application['route_id']
    route_query = select(m.routes.c.id, m.routes.c.provider, m.routes.c.url).where(
        m.routes.c.job_id == job['id'], m.routes.c.company_id == job['company_id'])
    if route_id is not None:
        route_query = route_query.where(m.routes.c.id == route_id)
    route = _one(connection, route_query)
    if route:
        context['ats_provider'] = route['provider']
    context.update(_posting_context(connection, job['id'], route))
    return context


def _application_context(connection, operation, params):
    application = _one(connection, select(m.applications).where(
        m.applications.c.id == operation['entity_id']))
    if application is None or not _profile_exists(connection, application['profile_id']):
        return {}
    # Legacy APPLY_MOCK rows predate a profile hint; their application FK owns it.
    # When scope hints are present, every one must agree with persisted ownership.
    for field in ('profile_id', 'application_id', 'route_id'):
        expected = application['id'] if field == 'application_id' else application[field]
        if field in params and params[field] != expected:
            return {}
    if operation['kind'] != 'APPLY_MOCK' and params.get('profile_id') != application['profile_id']:
        return {}
    job = _one(connection, select(m.jobs.c.id, m.jobs.c.company_id).where(
        m.jobs.c.id == application['job_id']))
    if job is None:
        return {}
    # A corrupt route must not be used to associate a different job/company.
    route = _one(connection, select(m.routes.c.id).where(
        m.routes.c.id == application['route_id'], m.routes.c.job_id == job['id'],
        m.routes.c.company_id == job['company_id']))
    if route is None:
        return {}
    return _job_context(connection, job, application)


def _prepare_context(connection, operation, params):
    profile_id = operation['entity_id']
    if not _profile_exists(connection, profile_id):
        return {}
    if 'profile_id' in params and params['profile_id'] != profile_id:
        return {}
    job_id = params.get('job_id')
    if not isinstance(job_id, str):
        return {}
    application = _one(connection, select(m.applications).where(
        m.applications.c.job_id == job_id, m.applications.c.profile_id == profile_id))
    matched = connection.execute(select(m.matches.c.id).where(
        m.matches.c.job_id == job_id, m.matches.c.profile_id == profile_id).limit(1)).first()
    if application is None and matched is None:
        return {}
    job = _one(connection, select(m.jobs.c.id, m.jobs.c.company_id).where(m.jobs.c.id == job_id))
    if job is None:
        return {}
    route_id = params.get('route_id')
    if application:
        saved_route = _one(connection, select(m.routes.c.id).where(
            m.routes.c.id == application['route_id'], m.routes.c.job_id == job_id,
            m.routes.c.company_id == job['company_id']))
        if saved_route is None:
            return {}
    if route_id is not None:
        route = _one(connection, select(m.routes.c.id).where(
            m.routes.c.id == route_id, m.routes.c.job_id == job_id,
            m.routes.c.company_id == job['company_id']))
        if route is None:
            return {}
        if application and application['route_id'] != route_id:
            application = None
    return _job_context(connection, job, application, route_id)


def _company_source_context(connection, source_id):
    if not isinstance(source_id, str):
        return {}
    row = _one(connection, select(
        m.company_sources.c.id, m.company_sources.c.company_id, m.company_sources.c.provider,
    ).join(m.companies, m.companies.c.id == m.company_sources.c.company_id).where(
        m.company_sources.c.id == source_id))
    return {**_source_context(row), 'company_id': row['company_id']} if row else {}


def _resolve(connection, operation):
    kind, params = operation['kind'], operation['params']
    if not isinstance(params, dict):
        return {}
    if kind in {'APPLY_MOCK', 'APPLICATION_FORM_REVIEW', 'APPLICATION_DRY_RUN',
                'UPLOADED_RESUME_COMPARE_QUEUED'}:
        return _application_context(connection, operation, params)
    if kind == 'PREPARE':
        return _prepare_context(connection, operation, params)
    if kind == 'DISCOVER':
        if not _profile_exists(connection, operation['entity_id']):
            return {}
        source_id = params.get('source_id')
        if not isinstance(source_id, str):
            return {}
        source = _one(connection, select(m.job_sources.c.id, m.job_sources.c.provider).where(
            m.job_sources.c.id == source_id))
        if source is None:
            return {}
        context = _source_context(source)
        # A collector may cover multiple companies. Never pick a global company.
        company = _one(connection, select(m.company_sources.c.company_id).where(
            m.company_sources.c.collector_source_id == source_id).distinct())
        if company:
            context['company_id'] = company['company_id']
        return context
    if kind == 'COMPANY_SOURCE_REFRESH':
        if 'source_id' in params and params['source_id'] != operation['entity_id']:
            return {}
        return _company_source_context(connection, operation['entity_id'])
    if kind == 'COMPANY_DISCOVER':
        # Reverse durable operation links; never trust candidate fields or URLs.
        request = _one(connection, select(
            m.company_discovery_requests.c.id,
            m.company_discovery_requests.c.company_id,
            m.company_discovery_requests.c.trigger_job_id,
            m.company_discovery_requests.c.candidate_id,
            m.company_discovery_requests.c.company_source_id,
            m.company_discovery_requests.c.profile_id,
        ).where(m.company_discovery_requests.c.operation_id == operation['id']))
        if request:
            if (request['company_id'] != operation['entity_id'] or
                    params.get('profile_id') != request['profile_id']):
                return {}
            if ('discovery_request_id' in params and params['discovery_request_id'] != request['id']):
                return {}
            context = _company_source_context(connection, request['company_source_id'])
            if context.get('company_id') != request['company_id']:
                return {}
            if request['profile_id'] is not None:
                trigger = _one(connection, select(
                    m.jobs.c.id.label('job_id'), m.postings.c.id.label('posting_id'),
                ).select_from(m.posting_value_candidates.join(
                    m.postings, m.posting_value_candidates.c.posting_id == m.postings.c.id,
                ).join(m.jobs, m.jobs.c.id == m.postings.c.job_id)).where(
                    m.posting_value_candidates.c.id == request['candidate_id'],
                    m.posting_value_candidates.c.company_source_id == request['company_source_id'],
                    m.jobs.c.id == request['trigger_job_id'],
                    m.jobs.c.company_id == request['company_id'],
                    select(m.matches.c.id).where(
                        m.matches.c.job_id == m.jobs.c.id,
                        m.matches.c.profile_id == request['profile_id'],
                    ).exists(),
                ))
                if trigger is None or not _profile_exists(connection, request['profile_id']):
                    return {}
                context.update(trigger)
            return context
        monitor = _one(connection, select(m.company_source_monitoring.c.source_id).where(
            m.company_source_monitoring.c.rediscovery_operation_id == operation['id']))
        if monitor:
            context = _company_source_context(connection, monitor['source_id'])
            return context if context.get('company_id') == operation['entity_id'] else {}
        if params.get('discovery_request_id') or params.get('monitoring_source_id'):
            return {}
        if params.get('profile_id') is not None and not _profile_exists(connection, params['profile_id']):
            return {}
        # Older discovery operations identify a company, without a source link.
        company = connection.execute(select(m.companies.c.id).where(
            m.companies.c.id == operation['entity_id'])).scalar_one_or_none()
        return {'company_id': company} if company else {}
    # Extraction has no job relationship; AI evaluation is a candidate batch.
    return {}


def resolve_operation_log_context(store, operation):
    """Reload durable identity, validate relationships, and return safe scalars only.

    The caller isolates lookup errors after committing the worker outcome. A new
    connection prevents failed enrichment from affecting its transaction.
    """
    with store.engine.connect() as connection:
        saved = _one(connection, select(m.operations).where(m.operations.c.id == operation.get('id')))
        if saved is None or any(saved[field] != operation.get(field) for field in ('kind', 'entity_id')):
            return {}
        return safe_operation_context(saved['kind'], _resolve(connection, saved))
