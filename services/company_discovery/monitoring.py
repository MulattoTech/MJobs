"""Durable schedules for exact, reviewed public employer feeds."""
import hashlib
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import now, upsert
from services.company_discovery.domains import VERIFIED_METHODS
from services.company_discovery.endpoints import machine_readable_job_endpoints, parse_public_job_feed
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError, HTTPStatusError
from services.company_discovery.public_resolution import resolve_public_company
from services.company_discovery.requests import _website
from services.company_discovery.sources import _url
from workers.dispatch import enqueue_operation
from workers.operations import OperationCancelled, timeout


FIELDS = ('company_id', 'provider', 'tenant', 'endpoint', 'career_url', 'collection_strategy', 'collector_source_id')
ACTIVE = {'QUEUED', 'RUNNING'}
MAX_RETRYABLE_SOURCE_FAILURES = 3


def _snapshot(source):
    return {key: source[key] for key in FIELDS}


def source_version(source):
    return hashlib.sha256(json.dumps(_snapshot(source), sort_keys=True).encode()).hexdigest()


def supported_source(source):
    return (not source['collector_source_id'] and source['tenant'] == source['company_id']
            and (source['provider'], source['collection_strategy']) in {('rss', 'RSS'), ('json_feed', 'PUBLIC_JSON')})


def _instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('MONITOR_TIME_INVALID')
    return result.astimezone(timezone.utc)


def _begin(connection):
    if connection.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def _personal(connection):
    if connection.execute(select(m.workspace_identity.c.operation_mode).where(
            m.workspace_identity.c.id == 'workspace')).scalar_one_or_none() != 'PERSONAL':
        raise ValueError('MONITOR_PERSONAL_WORKSPACE_REQUIRED')


def _source(connection, source_id, lock=False):
    query = select(m.company_sources).where(m.company_sources.c.id == source_id)
    row = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if row is None:
        raise ValueError('COMPANY_SOURCE_NOT_FOUND')
    return dict(row)


def _monitor(connection, source_id, lock=False):
    query = select(m.company_source_monitoring).where(m.company_source_monitoring.c.source_id == source_id)
    row = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    return dict(row) if row else None


def _authority(connection, source, authorization, expected_proof=None):
    _personal(connection)
    if not source['enabled']:
        raise ValueError('MONITOR_SOURCE_DISABLED')
    if not supported_source(source):
        raise ValueError('MONITOR_SOURCE_UNSUPPORTED')
    try:
        website = _website(source['career_url'])
        endpoint = _website(source['endpoint'])
    except (ValueError, TypeError):
        raise ValueError('MONITOR_ORIGIN_INVALID') from None
    career, feed = urlsplit(website), urlsplit(endpoint)
    if (career.scheme, career.hostname, career.port or 443) != (feed.scheme, feed.hostname, feed.port or 443):
        raise ValueError('MONITOR_ORIGIN_CHANGED')
    resolved = resolve_public_company(None, {'company_name': 'Stored employer', 'company_website': website}, connection=connection)
    if resolved['company_id'] != source['company_id']:
        raise ValueError('MONITOR_COMPANY_IDENTITY_CHANGED')
    verified = 'VERIFIED_DOMAIN_MATCH' in resolved['evidence']
    proof = []
    if verified:
        for row in connection.execute(select(m.company_domains).where(
                m.company_domains.c.company_id == source['company_id'],
                m.company_domains.c.domain == career.hostname, m.company_domains.c.verified.is_(True))
                .order_by(m.company_domains.c.id)).mappings():
            if row['verification_method'] in VERIFIED_METHODS and row['evidence']:
                value = {key: row[key] for key in ('id', 'company_id', 'domain', 'verification_method', 'evidence')}
                proof.append(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
    if authorization == 'VERIFIED_DOMAIN' and (not proof or expected_proof is not None and proof != expected_proof):
        raise ValueError('MONITOR_DOMAIN_AUTHORIZATION_CHANGED')
    if authorization not in {'VERIFIED_DOMAIN', 'USER_REVIEWED'}:
        raise ValueError('MONITOR_AUTHORIZATION_REQUIRED')
    return proof


def _profiles(connection):
    # A board-specific search is not permission to query unrelated employer sources.
    return sorted({row['profile_id'] for row in connection.execute(select(m.search_profiles).where(
        m.search_profiles.c.enabled.is_(True))).mappings() if not row['source_ids']})


def _active(connection, operation_id):
    return bool(operation_id and connection.execute(select(m.operations.c.state).where(
        m.operations.c.id == operation_id)).scalar_one_or_none() in ACTIVE)


def _create(connection, source, authorization, proof, interval, timestamp):
    due = _instant(source['last_success']) + timedelta(seconds=interval) if source['last_success'] else _instant(timestamp)
    row = dict(id=source['id'], source_id=source['id'], source_snapshot=_snapshot(source),
        authorization=authorization, domain_evidence=proof, revision=1, enabled=True, poll_interval=interval,
        status='IDLE', next_due=due.isoformat(), failure_count=0, created_at=timestamp, updated_at=timestamp)
    upsert(connection, m.company_source_monitoring, row)
    return _monitor(connection, source['id'], lock=True)


def configure_monitoring(store, source_id, expected_source_version, revision, poll_interval):
    if type(poll_interval) is not int or not 300 <= poll_interval <= 604800:
        raise ValueError('MONITOR_INTERVAL_INVALID')
    with store.engine.begin() as c:
        _begin(c)
        source = _source(c, source_id, lock=True)
        if source_version(source) != expected_source_version:
            raise ValueError('MONITOR_SOURCE_VERSION_CONFLICT')
        current = _monitor(c, source_id, lock=True)
        if type(revision) is not int or revision != (current['revision'] if current else 0):
            raise ValueError('MONITOR_REVISION_CONFLICT')
        if current and (_active(c, current['operation_id']) or _active(c, current['rediscovery_operation_id'])):
            raise ValueError('MONITOR_ACTIVE_OPERATION')
        proof = _authority(c, source, 'USER_REVIEWED')
        authorization = 'VERIFIED_DOMAIN' if proof else 'USER_REVIEWED'
        stamp = now()
        if current:
            c.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == current['id'])
                .values(source_snapshot=_snapshot(source), authorization=authorization, domain_evidence=proof,
                    revision=revision + 1, enabled=True, poll_interval=poll_interval, status='IDLE',
                    next_due=stamp, last_error=None, failure_count=0, updated_at=stamp))
        else:
            _create(c, source, authorization, proof, poll_interval, stamp)
        store._audit(c, source_id, 'COMPANY_SOURCE_MONITORING_REVIEWED', dict(company_source_id=source_id,
            source_version=expected_source_version, authorization=authorization, poll_interval=poll_interval))
        return _monitor(c, source_id)


def _validate(connection, source, monitor):
    if not monitor or not monitor['enabled']:
        raise ValueError('MONITOR_AUTHORIZATION_REQUIRED')
    if _snapshot(source) != monitor['source_snapshot']:
        raise ValueError('MONITOR_SOURCE_CHANGED')
    return _authority(connection, source, monitor['authorization'], monitor['domain_evidence'])


def _enqueue(connection, source, monitor, profiles, at, manual):
    params = dict(source_id=source['id'], monitoring_revision=monitor['revision'],
                  scheduled_for=monitor['next_due'], profile_ids=profiles, manual=manual,
                  predecessor_operation_id=monitor['operation_id'])
    operation_id = enqueue_operation(None, 'COMPANY_SOURCE_REFRESH', source['id'], params, connection=connection)
    connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == monitor['id'])
        .values(status='SCHEDULED', operation_id=operation_id, updated_at=at.isoformat()))
    return dict(source_id=source['id'], operation_id=operation_id)


def queue_refresh(store, source_id, profile_id):
    store.profile(profile_id)
    at = datetime.now(timezone.utc)
    with store.engine.begin() as c:
        _begin(c)
        source, monitor = _source(c, source_id, lock=True), _monitor(c, source_id, lock=True)
        _validate(c, source, monitor)
        if _active(c, monitor['operation_id']) or _active(c, monitor['rediscovery_operation_id']):
            raise ValueError('MONITOR_ACTIVE_OPERATION')
        if monitor['status'] in {'BLOCKED', 'REDISCOVERY_REQUIRED'}:
            raise ValueError('MONITOR_REVIEW_REQUIRED')
        if monitor['last_attempt'] and _instant(monitor['last_attempt']) + timedelta(seconds=300) > at:
            raise ValueError('MONITOR_COOLDOWN')
        return _enqueue(c, source, monitor, [profile_id], at, True)


def schedule_company_sources(store, at=None):
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError('MONITOR_TIME_INVALID')
    scheduled, skipped = [], []
    with store.engine.connect() as c:
        if c.execute(select(m.workspace_identity.c.operation_mode).where(
                m.workspace_identity.c.id == 'workspace')).scalar_one_or_none() != 'PERSONAL':
            return dict(scheduled=[], skipped=[])
        ids = list(c.execute(select(m.company_sources.c.id).where(m.company_sources.c.enabled.is_(True))
            .order_by(m.company_sources.c.id)).scalars())
    for source_id in ids:
        with store.engine.begin() as c:
            _begin(c)
            source = _source(c, source_id, lock=True)
            if not supported_source(source):
                continue
            monitor = _monitor(c, source_id, lock=True)
            try:
                if monitor is None:
                    proof = _authority(c, source, 'VERIFIED_DOMAIN')
                    monitor = _create(c, source, 'VERIFIED_DOMAIN', proof, 3600, at.isoformat())
                _validate(c, source, monitor)
            except (ValueError, PublicFetchError) as failure:
                if monitor and not _active(c, monitor['operation_id']):
                    c.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source_id)
                        .values(status='BLOCKED', last_error=str(failure), updated_at=at.isoformat()))
                skipped.append(dict(source_id=source_id, reason=str(failure)))
                continue
            if monitor['status'] in {'BLOCKED', 'REDISCOVERY_REQUIRED'}:
                continue
            if (monitor['status'] == 'FAILED'
                    and monitor['failure_count'] >= MAX_RETRYABLE_SOURCE_FAILURES):
                continue
            if _active(c, monitor['operation_id']) or _active(c, monitor['rediscovery_operation_id']):
                continue
            if monitor['status'] == 'SCHEDULED' and monitor['operation_id']:
                terminal = c.execute(select(m.operations.c.state).where(
                    m.operations.c.id == monitor['operation_id'])).scalar_one()
                c.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source_id)
                    .values(status='IDLE', last_error='OPERATION_' + terminal,
                        next_due=(at+timedelta(seconds=monitor['poll_interval'])).isoformat(), updated_at=at.isoformat()))
                continue
            profiles = _profiles(c)
            if not profiles:
                skipped.append(dict(source_id=source_id, reason='NO_UNRESTRICTED_SEARCH_PROFILE'))
                continue
            if monitor['next_due'] and _instant(monitor['next_due']) > at:
                continue
            scheduled.append(_enqueue(c, source, monitor, profiles, at, False))
        if len(scheduled) >= 20:
            break
    return dict(scheduled=scheduled, skipped=skipped)


def _authorize(connection, operation, rediscovery=False, lock_operation=False):
    params = operation['params']
    source_id = params.get('monitoring_source_id') if rediscovery else operation['entity_id']
    source, monitor = _source(connection, source_id), _monitor(connection, source_id)
    _validate(connection, source, monitor)
    pointer = 'rediscovery_operation_id' if rediscovery else 'operation_id'
    if monitor[pointer] != operation['id'] or monitor['revision'] != params.get('monitoring_revision'):
        raise ValueError('MONITOR_OPERATION_SUPERSEDED')
    query = select(m.operations).where(m.operations.c.id == operation['id'])
    current = connection.execute(query.with_for_update() if lock_operation else query).mappings().one()
    if current['state'] != 'RUNNING' or current['attempt_count'] != operation['attempt_count'] or current['cancel_requested']:
        raise ValueError('MONITOR_OPERATION_OWNERSHIP_CHANGED')
    if not current['lease_expires_at'] or _instant(current['lease_expires_at']) <= datetime.now(timezone.utc):
        raise ValueError('MONITOR_OPERATION_LEASE_EXPIRED')
    if not params.get('manual') and not rediscovery and not set(params['profile_ids']) & set(_profiles(connection)):
        raise ValueError('MONITOR_SEARCH_PAUSED')
    return source, monitor


def _lock_import_authority(connection, operation, rediscovery=False):
    from services.company_discovery.domains import lock_domain_evidence
    source_id = operation['params']['monitoring_source_id'] if rediscovery else operation['entity_id']
    source = _source(connection, source_id)
    host = urlsplit(source['career_url'] or '').hostname
    if host:
        lock_domain_evidence(connection, host)
    _source(connection, source_id, lock=True)
    _monitor(connection, source_id, lock=True)
    return _authorize(connection, operation, rediscovery=rediscovery, lock_operation=True)


def authorize_rediscovery(store, operation, *, connection=None):
    from contextlib import nullcontext
    try:
        with (nullcontext(connection) if connection is not None else store.engine.connect()) as c:
            source, _ = (_lock_import_authority(c, operation, rediscovery=True) if connection is not None
                          else _authorize(c, operation, rediscovery=True))
            if operation['params']['candidate']['company_website'] != source['career_url']:
                raise ValueError('MONITOR_ORIGIN_CHANGED')
    except ValueError as failure:
        raise PublicFetchError(str(failure)) from None


def refresh_company_source(store, operation, check=None):
    check = check or (lambda: None)
    source_id = operation['entity_id']
    params = operation['params']
    source = None
    imported = False
    import_attempted = False
    network_started = False
    feed_attempted = False
    available = None
    authorization_rejected = False
    def authorized():
        nonlocal authorization_rejected
        check()
        try:
            with store.engine.connect() as c:
                return _authorize(c, operation)
        except (ValueError, PublicFetchError):
            authorization_rejected = True
            raise
    try:
        # The durable receipt closes the import-commit / worker-acknowledgment gap.
        with store.engine.connect() as c:
            monitor = _monitor(c, source_id)
            if monitor and monitor['completed_operation_id'] == operation['id']:
                return
        source, monitor = authorized()
        with store.engine.begin() as c:
            _begin(c)
            _source(c, source_id, lock=True)
            _monitor(c, source_id, lock=True)
            _authorize(c, operation)
            c.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source_id)
                .values(last_attempt=now(), updated_at=now()))
        with closing(PublicCompanyClient(source['career_url'], check=authorized, total_timeout=min(90, timeout()-5))) as client:
            network_started = True
            careers = client.get(source['career_url'])
            if _url(str(careers.url), 'CAREER_URL') != source['career_url']:
                raise PublicFetchError('MONITOR_CAREER_URL_CHANGED')
            feeds = machine_readable_job_endpoints(str(careers.url), careers.text)
            if not any(_url(feed['url'], 'ENDPOINT') == source['endpoint'] and
                       feed['strategy'] == source['collection_strategy'] for feed in feeds):
                raise PublicFetchError('MONITOR_FEED_CHANGED')
            feed_attempted = True
            response = client.get(source['endpoint'])
            available = True
            if _url(str(response.url), 'ENDPOINT') != source['endpoint']:
                raise PublicFetchError('MONITOR_FEED_CHANGED')
            items = parse_public_job_feed(response.text, source['collection_strategy'])
            from services.company_discovery.application_links import sample_feed_links
            reports = sample_feed_links(items, source['collection_strategy'], source['endpoint'], client, check=authorized)
        profiles = params['profile_ids']
        if not params.get('manual'):
            with store.engine.connect() as c:
                profiles = sorted(set(profiles) & set(_profiles(c)))
        if not profiles:
            raise PublicFetchError('MONITOR_SEARCH_PAUSED')
        def import_guard(connection):
            _lock_import_authority(connection, operation)
            if not params.get('manual'):
                # Serialize pause/scope edits with the matches they authorize.
                # Every selected profile needs authority, not just one survivor.
                rows = connection.execute(select(m.search_profiles).where(
                    m.search_profiles.c.profile_id.in_(profiles))
                    .order_by(m.search_profiles.c.id).with_for_update()).mappings()
                eligible = {row['profile_id'] for row in rows if row['enabled'] and not row['source_ids']}
                if not set(profiles).issubset(eligible):
                    raise ValueError('MONITOR_SEARCH_PROFILE_CHANGED')
        def imported_receipt(connection):
            stamp = now()
            c_values = dict(status='HEALTHY', last_error=None, failure_count=0,
                completed_operation_id=operation['id'], next_due=(_instant(stamp) + timedelta(seconds=monitor['poll_interval'])).isoformat(), updated_at=stamp)
            connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source_id).values(**c_values))
            connection.execute(update(m.company_sources).where(m.company_sources.c.id == source_id).values(last_validation=stamp))
            store._audit(connection, source_id, 'COMPANY_SOURCE_REFRESHED', dict(company_source_id=source_id, operation_id=operation['id'], jobs_enumerated=len(items)))
        from services.discovery.company_feed import import_company_feed_jobs
        import_attempted = True
        import_company_feed_jobs(store, company_id=source['company_id'], source_id=source_id,
            provider=source['provider'], tenant=source['tenant'], endpoint=source['endpoint'],
            strategy=source['collection_strategy'], items=items, profile_id=profiles[0],
            check=check, fetch_available=True, link_reports=reports,
            import_guard=import_guard, import_receipt=imported_receipt, extra_profile_ids=profiles[1:])
        imported = True
    except Exception as failure:
        code = failure.code if isinstance(failure, PublicFetchError) else str(failure) if isinstance(failure, ValueError) and str(failure).isidentifier() else type(failure).__name__
        structural = code in {'MONITOR_FEED_CHANGED', 'MONITOR_CAREER_URL_CHANGED', 'COMPANY_JOB_FEED_INVALID', 'COMPANY_JOB_FEED_STRATEGY_INVALID', 'REDIRECT_BLOCKED'} or isinstance(failure, HTTPStatusError) and failure.status_code in {404,410}
        retryable = isinstance(failure, HTTPStatusError) and failure.status_code >= 500 or code in {'NETWORK_FAILED','FETCH_DEADLINE','DNS_TIMEOUT','DNS_FAILED','DNS_BUSY','DNS_EMPTY'}
        if not imported:
            with store.engine.begin() as c:
                _begin(c)
                current_source = _source(c, source_id, lock=True)
                current = _monitor(c, source_id, lock=True)
                live = c.execute(select(m.operations).where(m.operations.c.id == operation['id']).with_for_update()).mappings().one()
                if (current and current['operation_id'] == operation['id'] and current['revision'] == params.get('monitoring_revision')
                        and live['attempt_count'] == operation['attempt_count'] and live['state'] == 'RUNNING'):
                    cancelled = isinstance(failure, OperationCancelled) or live['cancel_requested']
                    failures = current['failure_count'] + (0 if cancelled else 1)
                    if cancelled:
                        state = 'IDLE'
                    elif structural:
                        state = 'REDISCOVERY_REQUIRED'
                    elif retryable:
                        state = ('IDLE' if failures < MAX_RETRYABLE_SOURCE_FAILURES
                                 else 'FAILED')
                    else:
                        state = 'BLOCKED'
                    rediscovery = None
                    if structural:
                        try:
                            _validate(c, current_source, current)
                            candidate = dict(company_name='Stored employer', company_website=current_source['career_url'])
                            rediscovery = enqueue_operation(None, 'COMPANY_DISCOVER', current_source['company_id'],
                                dict(candidate=candidate, fetch_mode='PUBLIC_HTTPS', monitoring_source_id=source_id,
                                     monitoring_revision=current['revision'], trigger_reason='KNOWN_SOURCE_BROKEN',
                                     request_id=operation['id'], profile_id=None), connection=c)
                        except (ValueError, PublicFetchError):
                            state = 'BLOCKED'
                    stamp = now()
                    if (network_started and not import_attempted and not cancelled and not authorization_rejected
                            and (not code.startswith('MONITOR_') or structural)):
                        from services.company_discovery.quality import record_observation
                        observed_available = False if feed_attempted and (isinstance(failure, HTTPStatusError) or retryable) else available
                        record_observation(c, company_source_id=source_id, source_endpoint=source['endpoint'],
                            succeeded=False, available=observed_available, error_code=code)
                    c.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source_id).values(
                        status=state, last_error=code[:128], failure_count=failures,
                        rediscovery_operation_id=rediscovery,
                        next_due=(_instant(stamp)+timedelta(seconds=min(86400,current['poll_interval']*2**min(failures,5)))).isoformat(), updated_at=stamp))
                    store._audit(c, source_id, 'COMPANY_SOURCE_REFRESH_FAILED', dict(company_source_id=source_id,
                        operation_id=operation['id'], error_code=code, status=state, rediscovery_operation_id=rediscovery))
        if isinstance(failure, ValueError):
            raise PublicFetchError(code) from None
        raise
