"""Bind supported public ATS inventories to verified official employer evidence."""
from sqlalchemy import or_, select, update

from packages.persistence import models as m
from packages.persistence.store import now
from services.ats_detection.board_links import greenhouse_board_tenants
from services.company_discovery.public_client import PublicFetchError
from services.company_discovery.public_resolution import resolve_public_company
from services.company_discovery.sources import register_ats_tenant, upsert_company_job_source
from services.company_discovery.source_host_locks import lock_company_source_hosts
from services.discovery.sources.greenhouse import SourceError, tenant_identifier


def greenhouse_tenant(career_url, html):
    """Use exact public board URLs in fetched markup, never names or script text."""
    try:
        tenants = greenhouse_board_tenants(career_url, html)
    except ValueError:
        raise PublicFetchError('COMPANY_ATS_TENANT_REVIEW_REQUIRED') from None
    if len(tenants) != 1:
        raise PublicFetchError('COMPANY_ATS_TENANT_REVIEW_REQUIRED')
    return tenants.pop()


def _endpoint(tenant):
    return 'https://boards-api.greenhouse.io/v1/boards/' + tenant_identifier(tenant) + '/jobs'


def bound_company_source(store, connection, source, *, lock=False):
    """Use ingestion -> domain -> source-host -> source-row -> collector ordering."""
    query = select(m.company_sources).where(or_(m.company_sources.c.collector_source_id == source['id'],
        (m.company_sources.c.provider == 'greenhouse') & (m.company_sources.c.tenant == source['tenant'])
        if source['provider'] == 'greenhouse' else False)).order_by(m.company_sources.c.id)
    rows = list(connection.execute(query).mappings())
    def bindings(records):
        return [row for row in records if row['collector_source_id'] == source['id']
                and row['discovered_automatically'] and row['career_url']]
    bound = bindings(rows)
    if len(bound) > 1:
        raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    initial = dict(bound[0]) if bound else None
    resolved = None
    if lock and source['provider'] == 'greenhouse':
        from packages.persistence.store import identity
        from services.dedupe.ingestion_identity import lock_company_ingestion
        lock_company_ingestion(connection, [initial['company_id'] if initial else identity('greenhouse', source['tenant'])])
        if initial:
            company = connection.execute(select(m.companies).where(m.companies.c.id == initial['company_id'])).mappings().one()
            resolved = resolve_public_company(store, {'company_name': company['name'],
                'company_website': initial['career_url']}, connection=connection, lock_verified=True)
        lock_company_source_hosts(connection, (_endpoint(source['tenant']), initial['career_url'] if initial else None))
        rows = list(connection.execute(query.with_for_update()).mappings())
        bound = bindings(rows)
        keys = ('id', 'company_id', 'provider', 'tenant', 'endpoint', 'career_url', 'collector_source_id')
        if len(bound) != (1 if initial else 0) or (initial and any(bound[0][key] != initial[key] for key in keys)):
            raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    if not bound:
        return None  # Legacy explicitly configured sources retain their original identities.
    if len(bound) != 1 or any(row['company_id'] != bound[0]['company_id'] for row in rows):
        raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    row = bound[0]
    if not row['enabled']:
        raise SourceError('COMPANY_SOURCE_DISABLED')
    if (row['provider'] != 'greenhouse' or source['provider'] != 'greenhouse'
            or row['tenant'] != source['tenant'] or row['endpoint'] != _endpoint(source['tenant'])
            or row['collection_strategy'] != 'API'):
        raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    if resolved is None:
        company = connection.execute(select(m.companies).where(m.companies.c.id == row['company_id'])).mappings().one()
        resolved = resolve_public_company(store, {'company_name': company['name'],
            'company_website': row['career_url']}, connection=connection)
    if resolved['company_id'] != row['company_id'] or 'VERIFIED_DOMAIN_MATCH' not in resolved['evidence']:
        raise SourceError('COMPANY_DOMAIN_REVIEW_REQUIRED')
    return dict(row)


def collector_view(store, connection, source):
    """Expose real recurring policy without inventing a scheduled operation/time."""
    if not source['collector_source_id'] or not source['discovered_automatically'] or not source['career_url']:
        return None
    from services.discovery.scheduler import _has_saved_search_scheduler, _scheduler_criteria
    collector = connection.execute(select(m.job_sources).where(
        m.job_sources.c.id == source['collector_source_id'])).mappings().one()
    status = 'ACTIVE'
    try:
        bound_company_source(store, connection, collector)
    except (SourceError, PublicFetchError):
        status = 'REVIEW_REQUIRED'
    profiles = connection.execute(select(m.search_profiles).where(m.search_profiles.c.enabled.is_(True))).mappings()
    if status == 'ACTIVE' and not any(
            (not row['source_ids'] or collector['id'] in row['source_ids'])
            and (not _has_saved_search_scheduler(row) or _scheduler_criteria(row) is not None) for row in profiles):
        status = 'NO_SCHEDULED_SEARCH'
    if not source['enabled'] or not collector['enabled']:
        status = 'PAUSED'
    return {'id': collector['id'], 'status': status, 'poll_interval': collector['poll_interval'],
            'health_status': collector['health_status']}


def enroll_greenhouse(store, candidate, resolved, career_url, html, *, profile_id,
                      profile_version=None, check, enrollment_guard=None, preserve_source_id=None):
    """Enroll once, then use the normal bounded collector, normalization and dedupe."""
    from services.discovery.sources.collect import configure_source, collect_source

    tenant = greenhouse_tenant(career_url, html)
    endpoint = _endpoint(tenant)
    check()
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        from services.dedupe.ingestion_identity import lock_company_ingestion
        lock_company_ingestion(connection, [resolved['company_id']])
        fresh = resolve_public_company(store, candidate, connection=connection, lock_verified=True)
        if fresh['company_id'] != resolved['company_id'] or 'VERIFIED_DOMAIN_MATCH' not in fresh['evidence']:
            raise PublicFetchError('COMPANY_DOMAIN_REVIEW_REQUIRED')
        lock_company_source_hosts(connection, (endpoint, career_url))
        if enrollment_guard:
            enrollment_guard(connection)
        # Existing collector policy is immutable during automatic enrollment.
        collector_id = configure_source(store, tenant, connection=connection, preserve_existing=True)
        collector = connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == collector_id).with_for_update()).mappings().one()
        if not collector['enabled']:
            raise PublicFetchError('COMPANY_SOURCE_DISABLED')
        # Never re-parent an already configured board's historical employer/jobs.
        owners = list(connection.execute(select(m.company_sources).where(
            m.company_sources.c.provider == 'greenhouse', m.company_sources.c.tenant == tenant)
            .with_for_update()).mappings())
        if any(row['company_id'] != fresh['company_id'] for row in owners):
            raise PublicFetchError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
        tenant_record = register_ats_tenant(connection, fresh['company_id'], provider='greenhouse',
            tenant_identifier=tenant, confidence=0.99,
            evidence=['Verified official career page: ' + career_url, 'Public Greenhouse board: ' + tenant])
        source = upsert_company_job_source(connection, company_id=fresh['company_id'],
            provider='greenhouse', tenant_identifier=tenant, career_url=career_url,
            public_api_url=endpoint, collection_strategy='API', confidence=0.99,
            enabled=True, discovered_automatically=True, ats_tenant_id=tenant_record['id'],
            last_validation=now(), profile_id=profile_id)
        if preserve_source_id and source['id'] != preserve_source_id:
            raise PublicFetchError('COMPANY_SOURCE_MIGRATION_REVIEW_REQUIRED')
        if not source['enabled']:
            raise PublicFetchError('COMPANY_SOURCE_DISABLED')
        connection.execute(update(m.company_sources).where(m.company_sources.c.id == source['id'])
                           .values(collector_source_id=collector_id))
        bound_company_source(store, connection, collector, lock=True)
        store._audit(connection, fresh['company_id'], 'COMPANY_SOURCE_DISCOVERED', {
            'company_source_id': source['id'], 'provider': 'greenhouse', 'tenant': tenant,
            'careers_url': career_url, 'endpoint': endpoint,
            'discovery_method': 'VERIFIED_OFFICIAL_ATS_BOARD', 'collection_strategy': 'API'})
    check()
    # Recurrence uses this same persisted binding; no parallel importer or job schema.
    result = collect_source(store, collector_id, profile_id, profile_version, check=check,
                            import_guard=enrollment_guard)
    return {'company': fresh, 'company_source_id': source['id'], 'provider': 'greenhouse',
            'tenant': tenant, 'careers_url': career_url, 'endpoint': endpoint,
            'jobs_enumerated': result['jobs_seen'], 'jobs_imported': result['jobs_seen']}
