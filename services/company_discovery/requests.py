"""Transactional useful-job discovery intent and explicit candidate-origin review."""
import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4
from urllib.parse import urlsplit

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.company_discovery.domains import (
    VERIFIED_METHODS, is_aggregator_domain, lock_domain_evidence, normalize_domain,
)
from services.company_discovery.public_client import PublicFetchError, _parse_url
from services.company_discovery.public_resolution import resolve_public_company
from services.company_discovery.source_host_locks import lock_company_source_hosts
from services.company_discovery.sources import _url
from services.dedupe.ingestion_identity import lock_company_ingestion
from workers.dispatch import enqueue_operation


SHARED_ATS_HOSTS = {'boards.greenhouse.io', 'boards-api.greenhouse.io', 'job-boards.greenhouse.io',
                    'jobs.lever.co', 'api.lever.co', 'jobs.eu.lever.co', 'api.eu.lever.co',
                    'jobs.ashbyhq.com', 'api.ashbyhq.com'}
SHARED_ATS_SUFFIXES = {'myworkdayjobs.com', 'icims.com', 'smartrecruiters.com', 'applytojob.com'}
SOURCE_FIELDS = ('company_id', 'provider', 'tenant', 'endpoint', 'collector_source_id')
REVIEWABLE = {'NEEDS_ORIGIN_REVIEW', 'FAILED', 'BLOCKED', 'CANCELLED'}


def _audit(connection, request, action, **payload):
    connection.execute(m.audit.insert().values(id=uuid4().hex, entity_id=request['id'],
        action=action, payload={'revision': request['revision'], **payload}, timestamp=now()))


def _website(value):
    parsed = _parse_url(value)
    if parsed is None:
        raise ValueError('DISCOVERY_WEBSITE_INVALID')
    _, host, _ = parsed
    if (is_aggregator_domain(host) or host in SHARED_ATS_HOSTS or
            any(host == suffix or host.endswith('.' + suffix) for suffix in SHARED_ATS_SUFFIXES)):
        raise ValueError('DISCOVERY_EMPLOYER_ORIGIN_REQUIRED')
    # Preserve the explicitly supplied path; the public client owns DNS and redirects.
    return value


def _personal(connection):
    return connection.execute(select(m.workspace_identity.c.operation_mode).where(
        m.workspace_identity.c.id == 'workspace')).scalar_one_or_none() == 'PERSONAL'


def _origin(connection, company_id):
    rows = [dict(row) for row in connection.execute(select(m.company_domains).where(
        m.company_domains.c.company_id == company_id, m.company_domains.c.verified.is_(True)))
        .mappings() if row['verification_method'] in VERIFIED_METHODS and row['evidence']]
    if not rows:
        return None, []
    highest = max(row['confidence'] for row in rows)
    best = [row for row in rows if row['confidence'] == highest]
    if len({row['domain'] for row in best}) != 1:
        return None, []
    chosen = best[0]
    try:
        website = _website('https://' + chosen['domain'] + '/')
    except ValueError:
        return None, []
    owners = {row['company_id'] for row in connection.execute(select(m.company_domains).where(
        m.company_domains.c.domain == chosen['domain'], m.company_domains.c.verified.is_(True)))
        .mappings() if row['verification_method'] in VERIFIED_METHODS and row['evidence']}
    if owners != {company_id}:
        return None, []
    proof = {key: chosen[key] for key in ('id', 'company_id', 'domain', 'verification_method', 'evidence')}
    checksum = hashlib.sha256(json.dumps(proof, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return website, [dict(domain_id=chosen['id'], domain=chosen['domain'], sha256=checksum)]


def _source_snapshot_digest(request):
    return hashlib.sha256(json.dumps(request['source_snapshot'], sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _confirmation_record(request, website):
    record = dict(method='USER_CONFIRMED_DOMAIN', company_id=request['company_id'],
        request_id=request['id'], reviewed_revision=request['revision'], website=website,
        domain=normalize_domain(website), source_snapshot_sha256=_source_snapshot_digest(request))
    record['sha256'] = hashlib.sha256(json.dumps(record, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return record


def _lock_source_rows(connection, request, *, lock=True):
    source_query = select(m.company_sources).where(m.company_sources.c.id == request['company_source_id'])
    if lock and connection.dialect.name == 'postgresql':
        source_query = source_query.with_for_update()
    source = connection.execute(source_query).mappings().one_or_none()
    if source is None:
        raise ValueError('DISCOVERY_SOURCE_CHANGED')
    if source['collector_source_id']:
        collector_query = select(m.job_sources).where(m.job_sources.c.id == source['collector_source_id'])
        if lock and connection.dialect.name == 'postgresql':
            collector_query = collector_query.with_for_update()
        connection.execute(collector_query).mappings().one_or_none()
    return source


def _lock_source_owners_for_domain(connection, host, expected_company_id, *, request_source_id=None, lock=True):
    """Refuse a user attestation that conflicts with an already enrolled portal."""
    rows = connection.execute(select(m.company_sources).order_by(m.company_sources.c.id)).mappings().all()
    related = []
    for row in rows:
        row_hosts = [normalize_domain(row[key]) for key in
                     ('endpoint', 'career_url', 'feed_url', 'public_api_url', 'search_url') if row[key]]
        if any(value and (value == host or value.endswith('.' + host) or host.endswith('.' + value))
               for value in row_hosts):
            related.append(row)
    lock_ids = {row['id'] for row in related}
    if request_source_id:
        lock_ids.add(request_source_id)
    locked = related
    if lock and connection.dialect.name == 'postgresql' and lock_ids:
        locked = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id.in_(sorted(lock_ids)))
            .order_by(m.company_sources.c.id).with_for_update()).mappings().all()
    for row in locked:
        row_hosts = [normalize_domain(row[key]) for key in
                     ('endpoint', 'career_url', 'feed_url', 'public_api_url', 'search_url') if row[key]]
        if (row['company_id'] != expected_company_id and
                any(value and (value == host or value.endswith('.' + host) or host.endswith('.' + value))
                    for value in row_hosts)):
            raise ValueError('DISCOVERY_COMPANY_IDENTITY_CONFLICT')
    collector_ids = sorted({row['collector_source_id'] for row in locked if row['collector_source_id']})
    for collector_id in collector_ids:
        collector_query = select(m.job_sources).where(m.job_sources.c.id == collector_id)
        if lock and connection.dialect.name == 'postgresql':
            collector_query = collector_query.with_for_update()
        connection.execute(collector_query).mappings().one_or_none()
    return next((row for row in locked if row['id'] == request_source_id), None)


def _domain_evidence_rows(connection, host, *, lock=True):
    if lock:
        return lock_domain_evidence(connection, host)
    domain = normalize_domain(host)
    if not domain:
        raise ValueError('COMPANY_DOMAIN_INVALID')
    return [dict(row) for row in connection.execute(select(m.company_domains).where(
        m.company_domains.c.domain == domain).order_by(m.company_domains.c.id)).mappings()]


def _user_confirmation_proof(connection, request, *, lock=True):
    host = normalize_domain(request['selected_website'])
    if not host:
        return None
    rows = _domain_evidence_rows(connection, host, lock=lock)
    if any(row['company_id'] != request['company_id'] for row in rows):
        return None
    expected = _confirmation_record(dict(request, revision=request['revision'] - 1), request['selected_website'])
    if request['authorization'] != 'USER_CONFIRMED_DOMAIN' or request['domain_evidence'] != [expected]:
        return None
    audits = connection.execute(select(m.audit.c.payload).where(
        m.audit.c.entity_id == request['id'],
        m.audit.c.action == 'COMPANY_DISCOVERY_REQUEST_AUTHORIZED')).scalars().all()
    if not any(payload.get('operation_id') == request['operation_id']
               and payload.get('authorization') == 'USER_CONFIRMED_DOMAIN'
               and payload.get('website') == request['selected_website']
               and payload.get('domain_evidence') == [expected]
               and payload.get('reviewed_revision') == request['revision'] - 1
               and payload.get('source_snapshot_sha256') == _source_snapshot_digest(request)
               for payload in audits):
        return None
    return [expected]


def _source_authorization(request):
    return dict(kind='USER_CONFIRMED_DOMAIN', request_id=request['id'],
        revision=request['revision'], operation_id=request['operation_id'],
        website=request['selected_website'], proof_sha256=request['domain_evidence'][0]['sha256'])


def confirmed_source_domain(connection, source, *, lock=False):
    """Acquire domain authority before any source-host or row lock."""
    reference = source['identity_authorization']
    if not isinstance(reference, dict) or reference.get('kind') != 'USER_CONFIRMED_DOMAIN':
        raise ValueError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
    website = _website(reference.get('website'))
    host = normalize_domain(website)
    career_host = normalize_domain(source['career_url'])
    if not career_host or not (career_host == host or career_host.endswith('.' + host)):
        raise ValueError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
    _domain_evidence_rows(connection, host, lock=lock)
    return host


def source_binding_digest(source):
    fields = ('id', 'company_id', 'provider', 'tenant', 'endpoint', 'career_url', 'collector_source_id')
    return hashlib.sha256(json.dumps({key: source[key] for key in fields},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def authorize_confirmed_source(connection, source, *, lock=False):
    """Revalidate a completed review without promoting it to verified domain proof.

    A locking caller owns ingestion, domain and source-host locks in that order.
    The original request, audit and source provenance remain live revocation gates.
    """
    reference = source['identity_authorization']
    host = confirmed_source_domain(connection, source, lock=False)
    initial = connection.execute(select(m.company_discovery_requests).where(
        m.company_discovery_requests.c.id == reference.get('request_id'))).mappings().one_or_none()
    if initial is None or initial['company_id'] != source['company_id']:
        raise ValueError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
    _lock_source_owners_for_domain(connection, host, source['company_id'],
        request_source_id=initial['company_source_id'], lock=lock)
    query = select(m.company_discovery_requests).where(m.company_discovery_requests.c.id == initial['id'])
    request = connection.execute(query.with_for_update() if lock else query).mappings().one()
    if (not _personal(connection) or request['state'] != 'QUEUED'
            or request['company_id'] != source['company_id']
            or _user_confirmation_proof(connection, request, lock=False) is None
            or reference != dict(_source_authorization(request), binding_sha256=source_binding_digest(source))):
        raise ValueError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
    operation = connection.execute(select(m.operations).where(
        m.operations.c.id == request['operation_id'])).mappings().one_or_none()
    if (operation is None or operation['state'] not in {'RUNNING', 'COMPLETED'}
            or operation['kind'] != 'COMPANY_DISCOVER' or operation['entity_id'] != request['company_id']
            or not isinstance(operation['params'], dict)
            or not isinstance(operation['params'].get('candidate'), dict)
            or operation['params'].get('discovery_request_id') != request['id']
            or operation['params'].get('discovery_request_revision') != request['revision']
            or operation['params'].get('candidate', {}).get('company_website') != request['selected_website']):
        raise ValueError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
    _source(connection, request)
    return {'company_id': request['company_id'], 'evidence': ['USER_CONFIRMED_DOMAIN']}


def _source(connection, request):
    source = connection.execute(select(m.company_sources).where(
        m.company_sources.c.id == request['company_source_id'])).mappings().one()
    if {key: source[key] for key in SOURCE_FIELDS} != request['source_snapshot']:
        raise ValueError('DISCOVERY_SOURCE_CHANGED')
    if not source['enabled']:
        raise ValueError('DISCOVERY_SOURCE_DISABLED')
    if source['collector_source_id']:
        collector = connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == source['collector_source_id'])).mappings().one()
        if not collector['enabled']:
            raise ValueError('DISCOVERY_SOURCE_DISABLED')
        if collector['provider'] != source['provider'] or collector['tenant'] != source['tenant']:
            raise ValueError('DISCOVERY_SOURCE_CHANGED')
    candidate = connection.execute(select(m.posting_value_candidates, m.postings.c.job_id,
        m.jobs.c.company_id.label('job_company_id')).select_from(m.posting_value_candidates).join(m.postings,
        m.postings.c.id == m.posting_value_candidates.c.posting_id).join(m.jobs, m.jobs.c.id == m.postings.c.job_id).where(
        m.posting_value_candidates.c.id == request['candidate_id'])).mappings().one()
    if (candidate['job_id'] != request['trigger_job_id'] or candidate['job_company_id'] != request['company_id']
            or candidate['company_source_id'] != source['id']
            or _url(candidate['normalized']['attributes']['source_endpoint'], 'ENDPOINT') != source['endpoint']):
        raise ValueError('DISCOVERY_SOURCE_CHANGED')
    return source


def _status(connection, request):
    if request['operation_id']:
        return connection.execute(select(m.operations.c.state).where(
            m.operations.c.id == request['operation_id'])).scalar_one()
    return request['state']


def _queue(connection, request, website, authorization, evidence, *, audit_details=None):
    company = connection.execute(select(m.companies).where(m.companies.c.id == request['company_id'])).mappings().one()
    candidate = {'company_name': company['name'], 'company_website': website}
    if authorization == 'USER_CONFIRMED_DOMAIN':
        # This request-scoped identity binding is backed by the explicit review
        # record; it does not promote the domain to shared verified authority.
        resolved = {'company_id': request['company_id'], 'evidence': ['USER_CONFIRMED_DOMAIN']}
    else:
        resolved = resolve_public_company(None, candidate, connection=connection)
    if ('VERIFIED_DOMAIN_MATCH' in resolved['evidence'] and resolved['company_id'] != request['company_id']
            or authorization in {'VERIFIED_DOMAIN', 'USER_CONFIRMED_DOMAIN'}
            and resolved['company_id'] != request['company_id']):
        raise ValueError('DISCOVERY_COMPANY_IDENTITY_CONFLICT')
    revision = request['revision'] + 1
    trigger_reason = ('USER_PREFERRED_COMPANY' if 'USER_PREFERRED_COMPANY' in request['reasons'] else
                      'HIGH_MATCH_JOB' if 'HIGH_MATCH_JOB' in request['reasons'] else
                      'MATCH_ELIGIBLE_JOB' if 'MATCH_ELIGIBLE_JOB' in request['reasons'] else
                      'PREVIOUSLY_UNSEEN_COMPANY')
    params = dict(candidate=candidate, fetch_mode='PUBLIC_HTTPS',
        profile_id=request['profile_id'], profile_version=request['profile_version'],
        trigger_reason=trigger_reason,
        dispatch_priority='HIGH' if 'HIGH_MATCH_JOB' in request['reasons'] else 'NORMAL',
        discovery_request_id=request['id'], discovery_request_revision=revision)
    operation_id = enqueue_operation(None, 'COMPANY_DISCOVER', resolved['company_id'], params, connection=connection)
    connection.execute(update(m.company_discovery_requests).where(m.company_discovery_requests.c.id == request['id'])
        .values(revision=revision, selected_website=website, authorization=authorization,
                domain_evidence=evidence, operation_id=operation_id, state='QUEUED'))
    _audit(connection, request, 'COMPANY_DISCOVERY_REQUEST_AUTHORIZED', new_revision=revision,
           operation_id=operation_id, authorization=authorization, website=website,
           **(audit_details or {}))
    return {'status': 'QUEUED', 'operation_id': operation_id, 'revision': revision}


def record_discovery_requests(connection, job_ids, profile=None, *, new_company_ids=()):
    """Acquire coalesced request locks in one order after all job/match writes."""
    keys = []
    for job in connection.execute(select(m.jobs).where(m.jobs.c.id.in_(sorted(set(job_ids))))).mappings():
        selected = (job['attributes'].get('source_resolution') or {}).get('selected_candidate_id')
        if selected:
            source_id = connection.execute(select(m.posting_value_candidates.c.company_source_id).where(
                m.posting_value_candidates.c.id == selected)).scalar_one()
            keys.append((job['company_id'], source_id, job['id'], selected))
    for company_id, _, job_id, selected in sorted(keys):
        record_discovery_request(connection, job_id, profile, is_new_company=company_id in new_company_ids,
                                 expected_candidate_id=selected)


def record_discovery_request(connection, job_id, profile=None, *, is_new_company=False,
                             expected_candidate_id=None, explicit_reason=None):
    """No network or separate transaction: callers commit jobs, matches and intent together."""
    if explicit_reason not in {None, 'USER_PREFERRED_COMPANY'}:
        raise ValueError('DISCOVERY_TRIGGER_INVALID')
    if not _personal(connection):
        return None
    job = connection.execute(select(m.jobs).where(m.jobs.c.id == job_id)).mappings().one()
    if job['attributes'].get('fictional'):
        return None
    selected = (job['attributes'].get('source_resolution') or {}).get('selected_candidate_id')
    if not selected:
        return None
    if expected_candidate_id is not None and selected != expected_candidate_id:
        # A concurrent collector owns the new winner; never change the bulk lock order.
        return None
    candidate = connection.execute(select(m.posting_value_candidates).where(
        m.posting_value_candidates.c.id == selected)).mappings().one()
    source = connection.execute(select(m.company_sources).where(
        m.company_sources.c.id == candidate['company_source_id'])).mappings().one()
    website, _ = _origin(connection, job['company_id'])
    if website and source['enabled'] and source['last_success']:
        endpoint = urlsplit(source['endpoint'])
        employer = urlsplit(website)
        if (endpoint.scheme, endpoint.netloc) == (employer.scheme, employer.netloc):
            # This exact employer origin already supplies successful direct reads.
            # Shared ATS board origins deliberately do not satisfy this check.
            return None
    score = None
    if profile:
        score = connection.execute(select(m.matches.c.score).where(m.matches.c.job_id == job_id,
            m.matches.c.profile_id == profile['id'], m.matches.c.profile_version == profile['version'])).scalar_one_or_none()
    preferences = (profile or {}).get('preferences') or {}
    policy = preferences.get('company_discovery') or {}
    if policy.get('company_discovery_enabled', True) is not True:
        return None
    minimum_match = policy.get('minimum_match_for_discovery', 60)
    priority_match = policy.get('priority_match_for_discovery', 80)
    hourly_cap = policy.get('max_new_company_discoveries_per_hour', 12)
    reasons = ['PREVIOUSLY_UNSEEN_COMPANY'] if is_new_company else []
    if explicit_reason:
        reasons.append(explicit_reason)
    if score is not None and score >= minimum_match:
        reasons.append('MATCH_ELIGIBLE_JOB')
    if score is not None and score >= priority_match:
        reasons.append('HIGH_MATCH_JOB')
    if not reasons:
        return None
    request_id = identity('company-discovery-request', job['company_id'], source['id'])
    timestamp = now()
    if profile and connection.execute(select(m.company_discovery_requests.c.id).where(
            m.company_discovery_requests.c.id == request_id)).scalar_one_or_none() is None:
        cutoff = (datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                  - timedelta(hours=1)).isoformat()
        recent_count = connection.execute(select(m.company_discovery_requests.c.id).where(
            m.company_discovery_requests.c.profile_id == profile['id'],
            m.company_discovery_requests.c.created_at >= cutoff,
        )).scalars().all()
        if len(recent_count) >= hourly_cap:
            return None
    upsert(connection, m.company_discovery_requests, dict(id=request_id, company_id=job['company_id'],
        company_source_id=source['id'], candidate_id=selected, trigger_job_id=job_id,
        profile_id=profile['id'] if profile else None, profile_version=profile['version'] if profile else None,
        reasons=reasons, highest_match_score=score, created_at=timestamp, last_seen_at=timestamp,
        state='NEEDS_ORIGIN_REVIEW', revision=1, domain_evidence=[],
        source_snapshot={key: source[key] for key in SOURCE_FIELDS}))
    request = dict(connection.execute(select(m.company_discovery_requests).where(
        m.company_discovery_requests.c.id == request_id).with_for_update()).mappings().one())
    snapshot = {key: source[key] for key in SOURCE_FIELDS}
    if (request['source_snapshot'] != snapshot and request['state'] != 'DISMISSED'
            and _status(connection, request) not in {'QUEUED', 'RUNNING'}):
        fresh = dict(request, candidate_id=selected, trigger_job_id=job_id, source_snapshot=snapshot)
        try:
            _source(connection, fresh)
        except ValueError:
            return request_id
        _audit(connection, request, 'COMPANY_DISCOVERY_REQUEST_RENEWED',
            previous_operation_id=request['operation_id'], previous_candidate_id=request['candidate_id'],
            previous_source_snapshot=request['source_snapshot'], previous_website=request['selected_website'],
            previous_authorization=request['authorization'], previous_domain_evidence=request['domain_evidence'])
        renewed = dict(candidate_id=selected, trigger_job_id=job_id, source_snapshot=snapshot,
            profile_id=profile['id'] if profile else None, profile_version=profile['version'] if profile else None,
            revision=request['revision'] + 1, selected_website=None, authorization=None,
            domain_evidence=[], operation_id=None, state='NEEDS_ORIGIN_REVIEW')
        connection.execute(update(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == request_id).values(**renewed))
        request.update(renewed)
    request['reasons'] = sorted(set(request['reasons']) | set(reasons))
    scores = [value for value in (score, request['highest_match_score']) if value is not None]
    connection.execute(update(m.company_discovery_requests).where(m.company_discovery_requests.c.id == request_id)
        .values(reasons=request['reasons'], highest_match_score=max(scores) if scores else None, last_seen_at=timestamp))
    if request['state'] == 'NEEDS_ORIGIN_REVIEW':
        website, evidence = _origin(connection, request['company_id'])
        if website:
            try:
                _source(connection, request)
            except ValueError:
                return request_id
            _queue(connection, request, website, 'VERIFIED_DOMAIN', evidence)
    return request_id


def list_discovery_requests(store):
    """Return the latest 100 coalesced requests with live operation state."""
    with store.engine.connect() as connection:
        rows = connection.execute(select(m.company_discovery_requests, m.companies.c.name.label('company_name'),
            m.jobs.c.title.label('job_title')).select_from(m.company_discovery_requests)
            .join(m.companies, m.companies.c.id == m.company_discovery_requests.c.company_id)
            .join(m.jobs, m.jobs.c.id == m.company_discovery_requests.c.trigger_job_id)
            .order_by(m.company_discovery_requests.c.last_seen_at.desc(), m.company_discovery_requests.c.id).limit(100)).mappings().all()
        result = []
        for row in rows:
            status = _status(connection, row)
            error = connection.execute(select(m.operations.c.error).where(m.operations.c.id == row['operation_id'])).scalar_one_or_none() if row['operation_id'] else None
            result.append({**{key: row[key] for key in ('id', 'revision', 'company_name', 'job_title', 'reasons',
                'highest_match_score', 'selected_website', 'authorization', 'operation_id', 'last_seen_at')},
                'status': status, 'reason': error or ('EMPLOYER_ORIGIN_UNVERIFIED' if status == 'NEEDS_ORIGIN_REVIEW' else None)})
        return result


def _locked_request(connection, request_id, revision, *, transaction_locked=False):
    if connection.dialect.name == 'sqlite' and not transaction_locked:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    if not _personal(connection):
        raise ValueError('DISCOVERY_PERSONAL_WORKSPACE_REQUIRED')
    row = connection.execute(select(m.company_discovery_requests).where(
        m.company_discovery_requests.c.id == request_id).with_for_update()).mappings().one_or_none()
    if row is None:
        raise ValueError('DISCOVERY_REQUEST_NOT_FOUND')
    if type(revision) is not int or revision != row['revision']:
        raise ValueError('DISCOVERY_REQUEST_REVISION_CONFLICT')
    return row


def approve_discovery_request(store, request_id, website, revision, confirm_company_identity=False):
    if type(confirm_company_identity) is not bool:
        raise ValueError('DISCOVERY_IDENTITY_CONFIRMATION_INVALID')
    website = _website(str(website).strip())
    with store.engine.begin() as connection:
        confirmation_rows = []
        if confirm_company_identity:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            # The request identity is immutable; this unlocked read only supplies
            # the first key in the global ingestion → domain → source → request order.
            initial = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request_id)).mappings().one_or_none()
            if initial is None:
                raise ValueError('DISCOVERY_REQUEST_NOT_FOUND')
            lock_company_ingestion(connection, [initial['company_id']])
            host = normalize_domain(website)
            confirmation_rows = lock_domain_evidence(connection, host)
            if any(row['company_id'] != initial['company_id'] for row in confirmation_rows):
                raise ValueError('DISCOVERY_COMPANY_IDENTITY_CONFLICT')
            lock_company_source_hosts(connection, [host])
            _lock_source_owners_for_domain(connection, host, initial['company_id'],
                request_source_id=initial['company_source_id'])
        row = _locked_request(connection, request_id, revision, transaction_locked=confirm_company_identity)
        if (confirm_company_identity and
                (row['company_id'] != initial['company_id'] or row['company_source_id'] != initial['company_source_id'])):
            raise ValueError('DISCOVERY_REQUEST_CHANGED')
        if _status(connection, row) not in REVIEWABLE:
            raise ValueError('DISCOVERY_REQUEST_NOT_REVIEWABLE')
        _source(connection, row)
        if confirm_company_identity:
            evidence = [_confirmation_record(row, website)]
            return _queue(connection, row, website, 'USER_CONFIRMED_DOMAIN', evidence,
                audit_details=dict(reviewed_revision=row['revision'],
                    confirmation='USER_ASSERTED_COMPANY_DOMAIN', independent_verification=False,
                    domain=normalize_domain(website), source_snapshot_sha256=_source_snapshot_digest(row),
                    domain_evidence=evidence))
        return _queue(connection, row, website, 'USER_CANDIDATE', [])


def dismiss_discovery_request(store, request_id, revision):
    with store.engine.begin() as connection:
        row = _locked_request(connection, request_id, revision)
        if _status(connection, row) != 'NEEDS_ORIGIN_REVIEW':
            raise ValueError('DISCOVERY_REQUEST_NOT_REVIEWABLE')
        connection.execute(update(m.company_discovery_requests).where(m.company_discovery_requests.c.id == request_id)
                           .values(state='DISMISSED', revision=revision + 1))
        _audit(connection, row, 'COMPANY_DISCOVERY_REQUEST_DISMISSED', new_revision=revision + 1)
    return {'status': 'DISMISSED'}


def authorize_discovery_request(store, operation, *, connection=None):
    """Queued parameters are historical evidence; authorize from live state before egress."""
    if connection is None:
        with store.engine.connect() as owned_connection:
            # This path is a standalone, observational egress check. It can run
            # while the import transaction already owns the identity locks, so
            # it must revalidate live evidence without reacquiring those locks.
            return _authorize_discovery_request(store, operation, owned_connection, lock=False)
    return _authorize_discovery_request(store, operation, connection, lock=True)


def _authorize_discovery_request(store, operation, connection, *, lock):
    params = operation['params']
    try:
        initial = connection.execute(select(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == params['discovery_request_id'])).mappings().one_or_none()
        if initial is None:
            raise ValueError('DISCOVERY_AUTHORIZATION_CHANGED')
        if lock:
            lock_company_ingestion(connection, [initial['company_id']])
        if initial['authorization'] in {'VERIFIED_DOMAIN', 'USER_CONFIRMED_DOMAIN'}:
            host = normalize_domain(initial['selected_website'])
            _domain_evidence_rows(connection, host, lock=lock)
            if initial['authorization'] == 'USER_CONFIRMED_DOMAIN':
                if lock:
                    lock_company_source_hosts(connection, [host])
                _lock_source_owners_for_domain(connection, host, initial['company_id'],
                    request_source_id=initial['company_source_id'], lock=lock)
            else:
                _lock_source_rows(connection, initial, lock=lock)
        else:
            _lock_source_rows(connection, initial, lock=lock)
        request_query = select(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == params['discovery_request_id'])
        if lock:
            request_query = request_query.with_for_update()
        row = connection.execute(request_query).mappings().one_or_none()
        if (not _personal(connection) or row is None
                or row['company_id'] != initial['company_id']
                or row['company_source_id'] != initial['company_source_id']
                or row['state'] != 'QUEUED'
                or row['revision'] != params.get('discovery_request_revision') or row['operation_id'] != operation['id']
                or row['selected_website'] != params.get('candidate', {}).get('company_website')):
            raise ValueError('DISCOVERY_AUTHORIZATION_CHANGED')
        _source(connection, row)
        _website(row['selected_website'])
        if row['authorization'] == 'VERIFIED_DOMAIN':
            website, evidence = _origin(connection, row['company_id'])
            if website != row['selected_website'] or evidence != row['domain_evidence']:
                raise ValueError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
        elif row['authorization'] == 'USER_CONFIRMED_DOMAIN':
            evidence = _user_confirmation_proof(connection, row, lock=lock)
            if evidence is None or evidence != row['domain_evidence']:
                raise ValueError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
        elif row['authorization'] != 'USER_CANDIDATE':
            raise ValueError('DISCOVERY_AUTHORIZATION_REQUIRED')
        if row['authorization'] == 'USER_CONFIRMED_DOMAIN':
            resolved = {'company_id': row['company_id']}
        else:
            resolved = resolve_public_company(None, params['candidate'], connection=connection)
        if resolved['company_id'] != operation['entity_id']:
            raise ValueError('DISCOVERY_COMPANY_IDENTITY_CHANGED')
        result = {'company_id': row['company_id'], 'authorization': row['authorization']}
        if row['authorization'] == 'USER_CONFIRMED_DOMAIN':
            result['source_authorization'] = _source_authorization(row)
        return result
    except ValueError as error:
        raise PublicFetchError(str(error)) from None
