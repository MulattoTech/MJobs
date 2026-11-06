"""Observe public jobs without replacing the selected fixture application.

Only the existing loopback API is called here. The operator's containment
callback must independently verify scheduler containment AND exclusive source
policy ownership for the whole observation. The source API has no revision/CAS;
a policy fingerprint is a change detector, never an atomic ownership claim.
The caller must retain containment if cleanup cannot safely disable its source.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


ENDPOINTS = {'wwr': 'https://weworkremotely.com/remote-jobs.rss',
             'remotive': 'https://remotive.com/api/remote-jobs'}
POLICY_FIELDS = ('provider', 'tenant', 'enabled', 'poll_interval', 'concurrency',
                 'request_timeout', 'rate_limit', 'jitter_seconds')
FINGERPRINT_FIELDS = ('id', *POLICY_FIELDS, 'strategy', 'capabilities')
TERMINAL = {'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'DEAD_LETTERED', 'DISCARDED'}
CHECK = 'same_profile_public_readonly_jobs_observed'


class ObservationError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise ObservationError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.tzinfo is not None, 'SOURCE_TIMESTAMP_INVALID')
    return parsed


def _source(request, provider):
    rows = [row for row in request('GET', '/sources')['sources']
            if row['provider'] == provider and row['tenant'] == 'default']
    _require(len(rows) <= 1, 'SOURCE_IDENTITY_AMBIGUOUS')
    return rows[0] if rows else None


def _policy_fingerprint(source):
    return _digest({key: source[key] for key in FINGERPRINT_FIELDS})


def _selection(request, baseline):
    profile_id = baseline['profile_id']
    profile = request('GET', '/profiles/' + quote(profile_id, safe=''))
    app = request('GET', '/profiles/' + quote(profile_id, safe='') + '/applications/' +
                  quote(baseline['application_id'], safe=''))
    _require(profile['id'] == profile_id and app['id'] == baseline['application_id']
        and app['profile_id'] == profile_id and app['target'] == 'MOCK', 'BASELINE_SELECTION_CHANGED')
    for key, actual in (('profile_version', profile['version']),
                        ('selected_application_job_id', app['job_id']),
                        ('selected_application_route_id', app['route_id'])):
        _require(key not in baseline or baseline[key] == actual, 'BASELINE_SELECTION_CHANGED')
    _require('confirmation' not in baseline or baseline['confirmation'] == app.get('confirmation'),
             'BASELINE_SELECTION_CHANGED')
    job = request('GET', '/jobs/' + quote(app['job_id'], safe=''), params={'profile_id': profile_id})
    routes = [route for route in job['routes'] if route['id'] == app['route_id']]
    _require(job['id'] == app['job_id'] and job['attributes'].get('fictional') is True
        and len(routes) == 1 and routes[0]['provider'] == 'mock-ats', 'BASELINE_SELECTION_CHANGED')
    return dict(profile_version=profile['version'], job_id=job['id'], route_id=app['route_id'],
        sha256=_digest({'profile': profile, 'application': app, 'job': job}))


def _public_jobs(request, source, operation, selection, profile_id, endpoint):
    rows = request('GET', '/jobs', params={'profile_id': profile_id,
        'job_source_id': source['id'], 'limit': 5})['jobs']
    _require(rows, 'PUBLIC_JOBS_NOT_OBSERVED')
    seen, observed = set(), []
    for row in rows:
        job = request('GET', '/jobs/' + quote(row['id'], safe=''), params={'profile_id': profile_id})
        attrs, match = job.get('attributes') or {}, job.get('match') or {}
        _require(job['id'] == row['id'] and job['id'] != selection['job_id'] and job['id'] not in seen
            and attrs.get('fictional') is False and attrs.get('provider') == source['provider']
            and attrs.get('source_id') == source['id'] and attrs.get('source_endpoint') == endpoint
            and attrs.get('submission_capability') == 'MANUAL_ONLY'
            and attrs.get('native_posting_id') is not None
            and match.get('profile_id') == profile_id and match.get('profile_version') == selection['profile_version'],
            'PUBLIC_JOB_PROVENANCE_INVALID')
        proofs = []
        for posting in job.get('postings', []):
            if posting.get('source') != endpoint:
                continue
            # Immutable snapshots can predate an identical repeated observation;
            # current posting fetch time must belong to this successful collection.
            if (posting.get('fetched_at') != source.get('last_attempt')
                    or _timestamp(posting['fetched_at']) < _timestamp(operation['started_at'])):
                continue
            raw_hash = _digest(posting.get('raw'))
            snapshots = [item for item in posting.get('snapshots', []) if item.get('sha256') == raw_hash]
            if not snapshots or not isinstance(posting.get('raw'), dict):
                continue
            proofs.append(dict(posting_id=posting['id'], fetched_at=posting['fetched_at'],
                raw_sha256=raw_hash, snapshots=[{key: item[key] for key in ('id', 'sha256', 'retrieved_at')}
                                             for item in snapshots]))
        _require(proofs, 'PUBLIC_JOB_PROVENANCE_INVALID')
        observed.append(dict(job_id=job['id'], native_posting_id=attrs['native_posting_id'],
            source_id=source['id'], source_endpoint=endpoint, fictional=False, postings=proofs))
        seen.add(job['id'])
    return observed


def run(base_url, baseline, evidence_path, *, acceptance_run_id, provider='wwr',
        containment_preflight, request=None, execution_scope='LIVE_PUBLIC_READ_ONLY',
        timeout_seconds=90, poll_interval=.25, monotonic=time.monotonic, sleep=time.sleep,
        utc_now=lambda: datetime.now(timezone.utc)):
    """Return and save a truthful receipt; never retry a collection or submit forms.

    containment_preflight(stage, context) returns scheduler_contained=True,
    source_policy_exclusive=True, source_collection_exclusive=True and stable
    nonempty owner/token strings. Exclusive collection includes manual clients
    and retry dispatchers, not only the scheduled polling loop. The callback is
    called at preflight, enable, dispatch and cleanup. The caller owns the lease
    and scheduler lifecycle; this helper never stops or starts host services.
    CONTROLLED_TRANSPORT checks helper behavior but cannot earn criterion 11 PASS.
    """
    url = urlsplit(base_url)
    if (url.scheme != 'http' or url.hostname not in {'localhost', '127.0.0.1', '::1'}
            or url.username is not None or url.password is not None or url.query or url.fragment
            or url.path not in ('', '/') or provider not in ENDPOINTS
            or execution_scope not in {'LIVE_PUBLIC_READ_ONLY', 'CONTROLLED_TRANSPORT'}
            or not callable(containment_preflight) or not 2 <= timeout_seconds <= 120
            or not .05 <= poll_interval <= 2):
        raise ValueError('PUBLIC_OBSERVATION_CONFIGURATION_INVALID')
    continuity = {key: baseline.get(key) for key in ('profile_id', 'application_id', 'operation_id', 'request_id')}
    if (not all(isinstance(value, str) and value for value in continuity.values())
            or not isinstance(acceptance_run_id, str) or not acceptance_run_id
            or baseline.get('external_submission_enabled') is not False
            or not isinstance(baseline.get('source'), dict) or not baseline['source'].get('commit')):
        raise ValueError('PUBLIC_OBSERVATION_BASELINE_INVALID')
    evidence = dict(schema_version=1, result='FAIL', acceptance_run_id=acceptance_run_id,
        timestamp=utc_now().isoformat(), source=baseline['source'], **continuity,
        execution_scope=execution_scope, evidence_kind='PUBLIC_READ_ONLY', provider=provider,
        endpoint=ENDPOINTS[provider], external_submission_enabled=False,
        observed_real_job_ids=[], observations=[], cleanup={'status': 'NOT_REQUIRED'},
        inventory_scope='AT_MOST_FIVE_CURRENT_COLLECTION_JOBS',
        limitations=['Observed public jobs are distinct from the selected mock application.',
                    'No real application form or submission is exercised.',
                    'Source policy ownership is operator verified; the API provides no atomic policy CAS.'])
    owned_source = fingerprint = initial_selection = ownership = None
    enable_attempted = False
    with (httpx.Client(base_url=base_url, timeout=15, trust_env=False, follow_redirects=False)
          if request is None else nullcontext()) as client:
        if request is None:
            def request(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()

        def contained(stage):
            nonlocal ownership
            proof = containment_preflight(stage, dict(provider=provider, base_url=base_url,
                acceptance_run_id=acceptance_run_id, source_id=evidence.get('source_id') or owned_source,
                source_policy_sha256=evidence.get('source_policy_sha256'), newly_created=bool(owned_source)))
            _require(isinstance(proof, dict) and proof.get('scheduler_contained') is True
                and proof.get('source_policy_exclusive') is True
                and proof.get('source_collection_exclusive') is True
                and all(isinstance(proof.get(key), str) and proof[key] for key in ('owner', 'token')),
                'SCHEDULER_CONTAINMENT_REQUIRED')
            current = (proof['owner'], proof['token'])
            _require(ownership is None or ownership == current, 'SOURCE_OWNERSHIP_CHANGED')
            ownership = current
            evidence['containment'] = dict(owner=proof['owner'], token_sha256=_digest(proof['token']),
                scheduler_contained=True, source_policy_exclusive=True, source_collection_exclusive=True)

        try:
            settings = request('GET', '/settings')
            _require(settings.get('operation_mode') == 'FIXTURE'
                and settings.get('external_submission_enabled') is False, 'FIXTURE_READ_ONLY_REQUIRED')
            initial_selection = _selection(request, baseline)
            evidence.update(profile_version=initial_selection['profile_version'],
                selected_application_job_id=initial_selection['job_id'],
                selected_application_route_id=initial_selection['route_id'],
                selected_state_sha256=initial_selection['sha256'])
            contained('preflight')
            source = _source(request, provider)
            if source is None:
                contained('enable')
                _require(_source(request, provider) is None, 'SOURCE_APPEARED_DURING_PREFLIGHT')
                policy = dict(provider=provider, tenant='default', enabled=True, poll_interval=604800,
                    concurrency=1, request_timeout=10, rate_limit=1, jitter_seconds=0)
                enable_attempted = True
                configured = request('PUT', '/sources', json=policy)
                owned_source = configured['id']
                source = _source(request, provider)
                _require(source is not None and source['id'] == owned_source
                    and all(source[key] == value for key, value in policy.items()), 'SOURCE_CONFIGURATION_CHANGED')
                fingerprint = _policy_fingerprint(source)
            _require(source['enabled'] is True, 'SOURCE_DISABLED')
            now = utc_now()
            _require(not source.get('backoff_until') or _timestamp(source['backoff_until']) <= now, 'SOURCE_BACKOFF')
            _require(not source.get('last_attempt') or _timestamp(source['last_attempt']) +
                timedelta(seconds=source['poll_interval']) <= now, 'SOURCE_COOLDOWN')
            evidence.update(source_id=source['id'], source_policy_sha256=_policy_fingerprint(source))
            contained('dispatch')
            current = _source(request, provider)
            _require(current is not None and _policy_fingerprint(current) == evidence['source_policy_sha256']
                and current.get('last_attempt') == source.get('last_attempt')
                and current.get('backoff_until') == source.get('backoff_until'), 'SOURCE_CONFIGURATION_CHANGED')
            _require(_selection(request, baseline) == initial_selection, 'BASELINE_SELECTION_CHANGED')
            collection_request_id = 'public-observation-' + uuid4().hex
            queued = request('POST', '/sources/' + quote(source['id'], safe='') + '/collect',
                json=dict(profile_id=baseline['profile_id'], request_id=collection_request_id))
            operation_id = queued['operation_id']
            evidence.update(collection_operation_id=operation_id, collection_request_id=collection_request_id)
            deadline = monotonic() + timeout_seconds
            while True:
                operation = request('GET', '/operations/' + quote(operation_id, safe=''))
                params = operation.get('params') or {}
                _require(operation.get('id') == operation_id and operation.get('kind') == 'DISCOVER'
                    and operation.get('entity_id') == baseline['profile_id']
                    and params.get('source_id') == source['id'] and params.get('request_id') == collection_request_id
                    and params.get('profile_version') == initial_selection['profile_version'], 'COLLECTION_IDENTITY_CHANGED')
                error = operation.get('error_category') or operation.get('error')
                evidence.update(collection_state=operation['state'], collection_error=(error if
                    isinstance(error, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', error)
                    else 'UNCLASSIFIED_PROVIDER_FAILURE' if error else None))
                evidence['collection_operation_sha256'] = _digest(operation)
                if operation['state'] in TERMINAL:
                    break
                if monotonic() >= deadline:
                    cancelled = request('POST', '/operations/' + quote(operation_id, safe='') + '/cancel')
                    evidence['cancellation_requested'] = cancelled.get('cancel_requested') is True
                    raise ObservationError('PUBLIC_COLLECTION_TIMEOUT')
                sleep(poll_interval)
            _require(operation['state'] == 'COMPLETED', 'PUBLIC_COLLECTION_' + operation['state'])
            _require(operation.get('attempt_count') == 1 and operation.get('retry_count', 0) == 0,
                     'PUBLIC_COLLECTION_RETRIED')
            source = _source(request, provider)
            _require(source is not None and source['id'] == evidence['source_id']
                and source.get('last_success') == source.get('last_attempt'), 'SOURCE_RECEIPT_CHANGED')
            _require(_policy_fingerprint(source) == evidence['source_policy_sha256'], 'SOURCE_CONFIGURATION_CHANGED')
            evidence['source_record_sha256'] = _digest(source)
            _require(_selection(request, baseline) == initial_selection, 'BASELINE_SELECTION_CHANGED')
            evidence['observations'] = _public_jobs(request, source, operation, initial_selection,
                baseline['profile_id'], ENDPOINTS[provider])
            evidence['observed_real_job_ids'] = sorted(row['job_id'] for row in evidence['observations'])
            _require(request('GET', '/health').get('external_submission_enabled') is False,
                     'EXTERNAL_SUBMISSION_SETTING_CHANGED')
            evidence['result'] = 'PASS'
        except ObservationError as error:
            evidence['error_code'] = str(error)
        except Exception:
            evidence['error_code'] = 'PUBLIC_OBSERVATION_API_OR_RECORD_INVALID'
        finally:
            if enable_attempted and owned_source is None:
                evidence['cleanup'] = {'status': 'ENABLE_UNCONFIRMED_PRESERVE_CONTAINMENT'}
            if owned_source:
                try:
                    contained('cleanup')
                    current = _source(request, provider)
                    if current is None or fingerprint is None or _policy_fingerprint(current) != fingerprint:
                        evidence['cleanup'] = {'status': 'PRESERVED_POLICY_CHANGED'}
                    else:
                        policy = {key: current[key] for key in POLICY_FIELDS}
                        request('PUT', '/sources', json={**policy, 'enabled': False})
                        restored = _source(request, provider)
                        _require(restored is not None and restored['id'] == owned_source
                            and all(restored[key] == value for key, value in
                                    {**policy, 'enabled': False}.items()), 'SOURCE_RESTORE_UNCONFIRMED')
                        evidence['cleanup'] = {'status': 'RESTORED_DISABLED'}
                except ObservationError:
                    evidence['cleanup'] = {'status': 'PRESERVED_OWNERSHIP_CHANGED'}
                except Exception:
                    evidence['cleanup'] = {'status': 'RESTORE_UNCONFIRMED'}
                if evidence['cleanup']['status'] != 'RESTORED_DISABLED':
                    evidence['result'] = 'FAIL'
            if initial_selection:
                try:
                    _require(_selection(request, baseline) == initial_selection, 'BASELINE_SELECTION_CHANGED')
                except Exception:
                    evidence.update(result='FAIL', error_code='BASELINE_SELECTION_CHANGED')
    accepted = evidence['result'] == 'PASS' and execution_scope == 'LIVE_PUBLIC_READ_ONLY'
    evidence['acceptance_observations'] = [dict(step=11,
        status=('PASS' if accepted else 'NOT_RUN' if execution_scope == 'CONTROLLED_TRANSPORT' else 'FAIL'),
        observable_result=('Current public source jobs observed for the unchanged selected fixture profile/application.'
            if accepted else 'Controlled transport or failed observation does not establish live-source acceptance.'),
        evidence_kind='PUBLIC_READ_ONLY', source_commit=baseline['source']['commit'], run_id=acceptance_run_id,
        continuity=continuity, checks=[dict(name=CHECK, passed=accepted)])]
    path = Path(evidence_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return evidence
