"""Bind supported public ATS inventories to verified official employer evidence."""
from sqlalchemy import or_, select, update

from packages.persistence import models as m
from packages.persistence.store import now
from services.ats_detection.board_links import greenhouse_board_tenants, lever_board_tenants
from services.company_discovery.public_client import PublicFetchError
from services.company_discovery.public_resolution import resolve_public_company
from services.company_discovery.sources import register_ats_tenant, upsert_company_job_source
from services.company_discovery.source_host_locks import lock_company_source_hosts
from services.company_discovery.requests import (
    confirmed_source_domain, authorize_confirmed_source, source_binding_digest,
)
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


def _endpoint(tenant, provider='greenhouse'):
    if provider == 'lever':
        from services.discovery.sources.lever import endpoint
        return endpoint(tenant)
    return 'https://boards-api.greenhouse.io/v1/boards/' + tenant_identifier(tenant) + '/jobs'


def _owner_clause(provider, tenant):
    clause = m.company_sources.c.tenant == tenant
    if provider == 'lever':
        # Legacy/manual bare-site records have ambiguous region semantics. Do
        # not silently migrate or bypass their ownership and disabled policy.
        clause = or_(clause, m.company_sources.c.tenant == tenant.split(':',1)[1],
                     m.company_sources.c.endpoint == _endpoint(tenant,provider))
    return (m.company_sources.c.provider == provider) & clause


def bound_company_source(store, connection, source, *, lock=False):
    """Use ingestion -> domain -> source-host -> source-row -> collector ordering."""
    query = select(m.company_sources).where(or_(m.company_sources.c.collector_source_id == source['id'],
        _owner_clause(source['provider'],source['tenant'])
        if source['provider'] in {'greenhouse','lever'} else False)).order_by(m.company_sources.c.id)
    rows = list(connection.execute(query).mappings())
    def bindings(records):
        return [row for row in records if row['collector_source_id'] == source['id']
                and row['discovered_automatically'] and row['career_url']]
    bound = bindings(rows)
    if len(bound) > 1:
        raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    initial = dict(bound[0]) if bound else None
    resolved = None
    if lock and source['provider'] in {'greenhouse','lever'}:
        from packages.persistence.store import identity
        from services.dedupe.ingestion_identity import lock_company_ingestion
        lock_company_ingestion(connection, [initial['company_id'] if initial else identity(source['provider'], source['tenant'])])
        if initial:
            if initial['identity_authorization'] is not None:
                try:
                    confirmed_source_domain(connection, initial, lock=True)
                except ValueError as error:
                    raise SourceError(str(error)) from None
            else:
                company = connection.execute(select(m.companies).where(m.companies.c.id == initial['company_id'])).mappings().one()
                resolved = resolve_public_company(store, {'company_name': company['name'],
                    'company_website': initial['career_url']}, connection=connection, lock_verified=True)
        lock_company_source_hosts(connection, (_endpoint(source['tenant'],source['provider']), initial['career_url'] if initial else None))
        if initial and initial['identity_authorization'] is not None:
            try:
                resolved = authorize_confirmed_source(connection, initial, lock=True)
            except ValueError as error:
                raise SourceError(str(error)) from None
        rows = list(connection.execute(query.with_for_update()).mappings())
        bound = bindings(rows)
        keys = ('id', 'company_id', 'provider', 'tenant', 'endpoint', 'career_url', 'collector_source_id', 'identity_authorization')
        if len(bound) != (1 if initial else 0) or (initial and any(bound[0][key] != initial[key] for key in keys)):
            raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    if not bound:
        return None  # Legacy explicitly configured sources retain their original identities.
    if len(bound) != 1 or any(row['company_id'] != bound[0]['company_id'] for row in rows):
        raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    row = bound[0]
    if not row['enabled']:
        raise SourceError('COMPANY_SOURCE_DISABLED')
    if (source['provider'] not in {'greenhouse','lever'} or row['provider'] != source['provider']
            or row['tenant'] != source['tenant'] or row['endpoint'] != _endpoint(source['tenant'],source['provider'])
            or row['collection_strategy'] != 'API'):
        raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    if source['provider']=='lever' and any(record['tenant']!=source['tenant'] for record in rows):
        raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
    if resolved is None:
        if row['identity_authorization'] is not None:
            try:
                resolved = authorize_confirmed_source(connection, row)
            except ValueError as error:
                raise SourceError(str(error)) from None
        else:
            company = connection.execute(select(m.companies).where(m.companies.c.id == row['company_id'])).mappings().one()
            resolved = resolve_public_company(store, {'company_name': company['name'],
                'company_website': row['career_url']}, connection=connection)
    if (resolved['company_id'] != row['company_id'] or not
            {'VERIFIED_DOMAIN_MATCH', 'USER_CONFIRMED_DOMAIN'}.intersection(resolved['evidence'])):
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
            'health_status': collector['health_status'],
            'identity_basis': 'USER_CONFIRMED_DOMAIN' if source['identity_authorization'] is not None else 'VERIFIED_DOMAIN'}


def enroll_public_ats(store, candidate, resolved, career_url, html, *, profile_id,
                      profile_version=None, check, enrollment_guard=None, preserve_source_id=None,
                      identity_confirmation=None, provider='greenhouse'):
    """Enroll once, then use the normal bounded collector, normalization and dedupe."""
    from services.discovery.sources.collect import configure_source, collect_source

    if provider=='greenhouse':
        tenant = greenhouse_tenant(career_url,html)
    elif provider=='lever':
        try:
            tenants=lever_board_tenants(career_url,html)
        except ValueError:
            raise PublicFetchError('COMPANY_ATS_TENANT_REVIEW_REQUIRED') from None
        if len(tenants)!=1:
            raise PublicFetchError('COMPANY_ATS_TENANT_REVIEW_REQUIRED')
        tenant=tenants.pop()
    else:
        raise PublicFetchError('COMPANY_ATS_ENUMERATION_UNSUPPORTED')
    endpoint = _endpoint(tenant,provider)
    check()
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        from services.dedupe.ingestion_identity import lock_company_ingestion
        lock_company_ingestion(connection, [resolved['company_id']])
        if identity_confirmation:
            # Acquire domain before source-host locks, then require the live
            # operation admission guard; caller-supplied flags grant no authority.
            reference = identity_confirmation.get('source_authorization')
            confirmed_source_domain(connection, {'identity_authorization': reference,
                'career_url': career_url}, lock=True)
            fresh = resolved
        else:
            fresh = resolve_public_company(store, candidate, connection=connection, lock_verified=True)
            if fresh['company_id'] != resolved['company_id'] or 'VERIFIED_DOMAIN_MATCH' not in fresh['evidence']:
                raise PublicFetchError('COMPANY_DOMAIN_REVIEW_REQUIRED')
        lock_company_source_hosts(connection, (endpoint, career_url))
        authorization = enrollment_guard(connection) if enrollment_guard else None
        if identity_confirmation and (not authorization or authorization != identity_confirmation):
            raise PublicFetchError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
        # Existing collector policy is immutable during automatic enrollment.
        collector_id = configure_source(store, tenant, provider=provider, connection=connection, preserve_existing=True)
        collector = connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == collector_id).with_for_update()).mappings().one()
        if not collector['enabled']:
            raise PublicFetchError('COMPANY_SOURCE_DISABLED')
        # Never re-parent an already configured board's historical employer/jobs.
        owners = list(connection.execute(select(m.company_sources).where(
            _owner_clause(provider,tenant))
            .with_for_update()).mappings())
        if any(row['company_id'] != fresh['company_id'] for row in owners):
            raise PublicFetchError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
        if provider=='lever' and any(row['tenant']!=tenant for row in owners):
            raise PublicFetchError('COMPANY_SOURCE_DISABLED' if any(not row['enabled'] for row in owners)
                                   else 'COMPANY_SOURCE_MIGRATION_REVIEW_REQUIRED')
        tenant_record = register_ats_tenant(connection, fresh['company_id'], provider=provider,
            tenant_identifier=tenant, confidence=0.99,
            **({'organization_identifier': tenant.split(':')[0],
                'board_identifier': tenant.split(':')[1]} if provider == 'lever' else {}),
            evidence=[('User-confirmed employer career page: ' if identity_confirmation else
                'Verified official career page: ') + career_url, 'Public '+provider+' board: ' + tenant])
        source = upsert_company_job_source(connection, company_id=fresh['company_id'],
            provider=provider, tenant_identifier=tenant, career_url=career_url,
            public_api_url=endpoint, collection_strategy='API', confidence=0.99,
            enabled=True, discovered_automatically=True, ats_tenant_id=tenant_record['id'],
            last_validation=now(), profile_id=profile_id)
        if preserve_source_id and source['id'] != preserve_source_id:
            raise PublicFetchError('COMPANY_SOURCE_MIGRATION_REVIEW_REQUIRED')
        if not source['enabled']:
            raise PublicFetchError('COMPANY_SOURCE_DISABLED')
        values = dict(collector_source_id=collector_id, identity_authorization=None)
        if identity_confirmation:
            values['identity_authorization'] = dict(reference,
                binding_sha256=source_binding_digest(dict(source, collector_source_id=collector_id)))
        connection.execute(update(m.company_sources).where(m.company_sources.c.id == source['id']).values(**values))
        bound_company_source(store, connection, collector, lock=True)
        store._audit(connection, fresh['company_id'], 'COMPANY_SOURCE_DISCOVERED', {
            'company_source_id': source['id'], 'provider': provider, 'tenant': tenant,
            'careers_url': career_url, 'endpoint': endpoint,
            'discovery_method': 'USER_CONFIRMED_ATS_BOARD' if identity_confirmation else 'VERIFIED_OFFICIAL_ATS_BOARD',
            'independent_verification': not bool(identity_confirmation), 'collection_strategy': 'API'})
    check()
    # Recurrence uses this same persisted binding; no parallel importer or job schema.
    result = collect_source(store, collector_id, profile_id, profile_version, check=check,
                            import_guard=enrollment_guard)
    return {'company': fresh, 'company_source_id': source['id'], 'provider': provider,
            'tenant': tenant, 'careers_url': career_url, 'endpoint': endpoint,
            'jobs_enumerated': result['jobs_seen'], 'jobs_imported': result['jobs_seen']}


# Existing internal callers retain the default Greenhouse provider contract.
enroll_greenhouse = enroll_public_ats
