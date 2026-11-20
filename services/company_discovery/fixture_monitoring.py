"""Reviewed direct-employer monitoring confined to a persisted FIXTURE workspace.

This does not authorize public-company monitoring or application submission.
COMPANY_DISCOVER supplies the existing durable outbox/worker queue contract;
its explicit fixture_monitoring marker must be dispatched to refresh_fixture_source.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import now, upsert
from services.company_discovery.monitoring import source_version, FIELDS
from workers.dispatch import enqueue_operation


MARKER = 'direct-company-source-v1'
RECEIPT = 'FIXTURE_COMPANY_SOURCE_REFRESHED'
ACTIVE = {'QUEUED', 'RUNNING'}


def _instant(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('FIXTURE_MONITOR_TIME_INVALID')
    return stamp.astimezone(timezone.utc)


def _origin(value):
    try:
        parsed = urlsplit(value)
        hosts = {'localhost', '127.0.0.1'} | ({'mock'} if os.environ.get('MJOBS_COMPOSE_MOCK') == '1' else set())
        if (parsed.scheme != 'http' or parsed.hostname not in hosts or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or any(ord(c) < 33 for c in value) or not 1 <= (parsed.port or 80) <= 65535):
            raise ValueError()
        return value.rstrip('/')
    except (TypeError, ValueError):
        raise ValueError('FIXTURE_MONITOR_ORIGIN_INVALID') from None


def _begin(connection):
    if connection.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def _workspace(connection):
    mode = connection.execute(select(m.workspace_identity.c.operation_mode).where(
        m.workspace_identity.c.id == 'workspace')).scalar_one_or_none()
    if mode != 'FIXTURE':
        raise ValueError('FIXTURE_MONITOR_WORKSPACE_REQUIRED')


def _row(connection, table, identifier, *, lock=False):
    query = select(table).where(table.c.id == identifier)
    row = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if row is None:
        raise ValueError('FIXTURE_MONITOR_RECORD_NOT_FOUND')
    return dict(row)


def _search_fingerprint(connection, profile_id, *, lock=False):
    query = select(m.search_profiles).where(m.search_profiles.c.profile_id == profile_id).order_by(m.search_profiles.c.id)
    rows = connection.execute(query.with_for_update() if lock else query).mappings()
    eligible = [dict(id=row['id'], criteria=row['criteria']) for row in rows if row['enabled'] and not row['source_ids']]
    if not eligible:
        raise ValueError('FIXTURE_MONITOR_SEARCH_PAUSED')
    return hashlib.sha256(json.dumps(eligible, sort_keys=True).encode()).hexdigest()


def _authority(connection, origin, profile_id, source_id, *, lock=False):
    _workspace(connection)
    origin = _origin(origin)
    profile = _row(connection, m.profiles, profile_id, lock=lock)
    source = _row(connection, m.company_sources, source_id, lock=lock)
    if (not source['enabled'] or source['provider'] != 'mock-ats'
            or source['collection_strategy'] != 'FIXTURE' or source['collector_source_id']):
        raise ValueError('FIXTURE_MONITOR_SOURCE_UNSUPPORTED')
    expected = urlsplit(origin)
    for key in ('career_url', 'endpoint'):
        parsed = urlsplit(source[key] or '')
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('FIXTURE_MONITOR_SOURCE_ORIGIN_CHANGED')
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', source['tenant'])
            or source['endpoint'] != origin + '/ats/' + source['tenant'] + '/jobs'):
        raise ValueError('FIXTURE_MONITOR_DIRECT_ENDPOINT_REQUIRED')
    associated = connection.execute(select(m.audit.c.id).where(
        m.audit.c.entity_id == profile_id,
        m.audit.c.action.in_(('COMPANY_DISCOVERED', 'COMPANY_SOURCE_ENROLLED')),
        m.audit.c.payload['company_id'].as_string() == source['company_id']).limit(1)).first()
    if not associated:
        raise ValueError('FIXTURE_MONITOR_PROFILE_SOURCE_MISMATCH')
    snapshot = {key: source[key] for key in FIELDS}
    snapshot['fixture'] = dict(marker=MARKER, origin=origin, profile_id=profile_id,
        profile_version=profile['version'], search_fingerprint=_search_fingerprint(connection, profile_id, lock=lock))
    return source, snapshot


def configure_fixture_monitoring(store, origin, profile_id, source_id, expected_source_version, revision, poll_interval):
    if type(poll_interval) is not int or not 300 <= poll_interval <= 604800:
        raise ValueError('FIXTURE_MONITOR_INTERVAL_INVALID')
    if type(revision) is not int or revision < 0:
        raise ValueError('FIXTURE_MONITOR_REVISION_CONFLICT')
    with store.engine.begin() as connection:
        _begin(connection)
        source, snapshot = _authority(connection, origin, profile_id, source_id, lock=True)
        if source_version(source) != expected_source_version:
            raise ValueError('FIXTURE_MONITOR_SOURCE_VERSION_CONFLICT')
        current = connection.execute(select(m.company_source_monitoring).where(
            m.company_source_monitoring.c.id == source_id).with_for_update()).mappings().one_or_none()
        if revision != (current['revision'] if current else 0):
            raise ValueError('FIXTURE_MONITOR_REVISION_CONFLICT')
        if current and current['operation_id']:
            operation = _row(connection, m.operations, current['operation_id'])
            if operation['state'] in ACTIVE:
                raise ValueError('FIXTURE_MONITOR_ACTIVE_OPERATION')
        stamp = now()
        due = max(_instant(stamp), _instant(source['last_success'] or stamp) + timedelta(seconds=poll_interval))
        if current and current['next_due']:
            due = max(due, _instant(current['next_due']))
        values = dict(source_snapshot=snapshot, authorization='USER_REVIEWED', domain_evidence=[],
            revision=revision+1, enabled=True, poll_interval=poll_interval, status='IDLE',
            next_due=due.isoformat(), last_error=None, failure_count=0, updated_at=stamp)
        if current:
            connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source_id).values(**values))
        else:
            upsert(connection, m.company_source_monitoring, dict(id=source_id, source_id=source_id,
                created_at=stamp, **values))
        store._audit(connection, source_id, 'FIXTURE_COMPANY_MONITORING_REVIEWED',
            dict(profile_id=profile_id, source_version=expected_source_version, revision=revision+1, poll_interval=poll_interval))
        return _public(_row(connection, m.company_source_monitoring, source_id))


def _validate(connection, origin, monitor, *, lock=False):
    binding = monitor['source_snapshot'].get('fixture', {})
    if binding.get('marker') != MARKER or not monitor['enabled']:
        raise ValueError('FIXTURE_MONITOR_REVIEW_REQUIRED')
    source, snapshot = _authority(connection, origin, binding.get('profile_id'), monitor['source_id'], lock=lock)
    if snapshot['fixture']['profile_version'] != binding.get('profile_version'):
        raise ValueError('FIXTURE_MONITOR_PROFILE_CHANGED')
    if snapshot != monitor['source_snapshot']:
        raise ValueError('FIXTURE_MONITOR_REVIEW_CHANGED')
    return source


def _public(monitor):
    fields = ('source_id', 'revision', 'enabled', 'poll_interval', 'status', 'next_due',
        'last_attempt', 'last_error', 'failure_count', 'operation_id', 'completed_operation_id')
    return {**{key: monitor[key] for key in fields}, **monitor['source_snapshot']['fixture'],
            'source_version': source_version(monitor['source_snapshot']), 'external_submission_enabled': False}


def read_fixture_monitoring(store, origin, profile_id, source_id):
    with store.engine.connect() as connection:
        monitor = _row(connection, m.company_source_monitoring, source_id)
        if monitor['source_snapshot'].get('fixture', {}).get('profile_id') != profile_id:
            raise ValueError('FIXTURE_MONITOR_PROFILE_SOURCE_MISMATCH')
        _validate(connection, origin, monitor)
        return _public(monitor)


def schedule_fixture_sources(store, origin, at=None):
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError('FIXTURE_MONITOR_TIME_INVALID')
    with store.engine.connect() as connection:
        mode = connection.execute(select(m.workspace_identity.c.operation_mode).where(m.workspace_identity.c.id == 'workspace')).scalar_one_or_none()
        if mode != 'FIXTURE':
            return dict(scheduled=[], skipped=[])
        rows = list(connection.execute(select(m.company_source_monitoring)).mappings())
    scheduled, skipped = [], []
    for initial in rows:
        if initial['source_snapshot'].get('fixture', {}).get('marker') != MARKER:
            continue
        with store.engine.begin() as connection:
            _begin(connection)
            # Profile -> source -> monitor -> operation is the import lock order.
            try:
                _validate(connection, origin, initial, lock=True)
                monitor = _row(connection, m.company_source_monitoring, initial['id'], lock=True)
                _validate(connection, origin, monitor)
            except ValueError as error:
                skipped.append(dict(source_id=initial['source_id'], reason=str(error)))
                continue
            if monitor['status'] == 'BLOCKED':
                continue
            previous = _row(connection, m.operations, monitor['operation_id']) if monitor['operation_id'] else None
            if previous and previous['state'] in ACTIVE:
                continue
            if previous and monitor['status'] == 'SCHEDULED':
                # The worker owns retries until terminal. Never redraw the same retry delay.
                failures = monitor['failure_count'] + 1
                blocked = previous['state'] != 'FAILED' or failures >= 3
                connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == monitor['id']).values(
                    status='BLOCKED' if blocked else 'FAILED', failure_count=failures,
                    last_error='FIXTURE_MONITOR_OPERATION_' + previous['state'],
                    next_due=(at + timedelta(seconds=monitor['poll_interval'] * 2 ** min(failures, 5))).isoformat(), updated_at=at.isoformat()))
                continue
            if not monitor['next_due'] or _instant(monitor['next_due']) > at:
                continue
            binding = monitor['source_snapshot']['fixture']
            params = dict(fixture_monitoring=MARKER, fixture_source_id=monitor['source_id'],
                profile_id=binding['profile_id'], profile_version=binding['profile_version'],
                fixture_origin=binding['origin'], monitoring_revision=monitor['revision'],
                source_version=source_version(monitor['source_snapshot']), scheduled_for=monitor['next_due'],
                predecessor_operation_id=monitor['operation_id'], fetch_mode='FIXTURE')
            operation_id = enqueue_operation(store, 'COMPANY_DISCOVER', monitor['source_id'], params,
                connection=connection, task_key=f"fixture-monitor:{monitor['id']}:{monitor['revision']}:{monitor['next_due']}")
            connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == monitor['id']).values(
                status='SCHEDULED', operation_id=operation_id, updated_at=at.isoformat()))
            scheduled.append(dict(source_id=monitor['source_id'], operation_id=operation_id, scheduled_for=monitor['next_due']))
        if len(scheduled) >= 20:
            break
    return dict(scheduled=scheduled, skipped=skipped)


def fixture_refresh_saved(connection, operation):
    params = operation.get('params') or {}
    if (operation.get('kind') != 'COMPANY_DISCOVER' or params.get('fixture_monitoring') != MARKER
            or params.get('fixture_source_id') != operation.get('entity_id')):
        return False
    return connection.execute(select(m.audit.c.id).where(m.audit.c.action == RECEIPT,
        m.audit.c.entity_id == operation['entity_id'], m.audit.c.payload['operation_id'].as_string() == operation['id'],
        m.audit.c.payload['profile_id'].as_string() == params.get('profile_id'),
        m.audit.c.payload['source_id'].as_string() == operation['entity_id'],
        m.audit.c.payload['params_sha256'].as_string() == _params_digest(params)).limit(1)).first() is not None


def _params_digest(params):
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


def _authorize(connection, origin, operation, *, lock=False):
    params = operation['params']
    if (operation['kind'] != 'COMPANY_DISCOVER' or params.get('fixture_monitoring') != MARKER
            or params.get('fixture_source_id') != operation['entity_id'] or params.get('fetch_mode') != 'FIXTURE'
            or params.get('fixture_origin') != _origin(origin)):
        raise ValueError('FIXTURE_MONITOR_OPERATION_INVALID')
    monitor = _row(connection, m.company_source_monitoring, operation['entity_id'])
    source = _validate(connection, origin, monitor, lock=lock)
    monitor = _row(connection, m.company_source_monitoring, operation['entity_id'], lock=lock)
    binding = monitor['source_snapshot']['fixture']
    if (monitor['status'] != 'SCHEDULED' or monitor['operation_id'] != operation['id'] or monitor['revision'] != params.get('monitoring_revision')
            or params.get('profile_id') != binding['profile_id'] or params.get('profile_version') != binding['profile_version']
            or params.get('source_version') != source_version(source) or params.get('scheduled_for') != monitor['next_due']):
        raise ValueError('FIXTURE_MONITOR_OPERATION_SUPERSEDED')
    current = _row(connection, m.operations, operation['id'], lock=lock)
    if (current['params'] != params or current['state'] != 'RUNNING' or current['cancel_requested']
            or current['attempt_count'] != operation['attempt_count'] or not current['lease_expires_at']
            or _instant(current['lease_expires_at']) <= datetime.now(timezone.utc)):
        raise ValueError('FIXTURE_MONITOR_OPERATION_OWNERSHIP_CHANGED')
    return source, monitor


def refresh_fixture_source(store, origin, operation, check=lambda: None):
    from services.discovery.pipeline import discover
    with store.engine.connect() as connection:
        if fixture_refresh_saved(connection, operation):
            return {'replayed': True}
        source, monitor = _authorize(connection, origin, operation)
    started = now()
    with store.engine.begin() as connection:
        _begin(connection)
        _authorize(connection, origin, operation, lock=True)
        connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source['id']).values(
            last_attempt=started, updated_at=started))
    def guard(connection):
        check()
        _authorize(connection, origin, operation, lock=True)
    def receipt(connection):
        stamp = now()
        connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source['id']).values(
            status='HEALTHY', failure_count=0, last_error=None,
            completed_operation_id=operation['id'], next_due=(_instant(stamp)+timedelta(seconds=monitor['poll_interval'])).isoformat(), updated_at=stamp))
        connection.execute(update(m.company_sources).where(m.company_sources.c.id == source['id']).values(last_success=stamp, last_validation=stamp))
        store._audit(connection, source['id'], RECEIPT, dict(source_id=source['id'], operation_id=operation['id'],
            profile_id=operation['params']['profile_id'], revision=monitor['revision'], params_sha256=_params_digest(operation['params'])))
    result = discover(store, origin, operation['params']['profile_id'], check=check,
        profile_version=operation['params']['profile_version'], company_source=source,
        import_guard=guard, import_receipt=receipt)
    return {'replayed': False, 'jobs': len(result)}
