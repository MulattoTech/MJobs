"""Observe public jobs without replacing the selected fixture application.

Only the existing loopback API is called here. The operator's containment
callback must independently verify scheduler containment AND exclusive source
policy ownership for the whole observation. The source API has no revision/CAS;
a policy fingerprint is a change detector, never an atomic ownership claim.
The caller must retain containment if cleanup cannot safely disable its source.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
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
API_TIMEOUT_SECONDS = 15
LEASE_CONTINUITY_MAX_CHECKS = 128
LEASE_CONTINUITY_MAX_RECORDS = 16
SAFE_CONTAINMENT_ERROR_CODES = frozenset({
    'CHILD_READBACK_PARENT_STATE_INVALID', 'CHILD_READBACK_NOT_FINAL',
    'CHILD_PARENT_OPERATION_MISSING', 'CHILD_PARENT_OPERATION_CHANGED',
    'CHILD_PARENT_NOT_AUTHORIZED', 'CHILD_PARENT_ATTEMPT_CHANGED',
    'CHILD_PARENT_PROFILE_VERSION_INVALID', 'CHILD_PARENT_IDENTITY_CHANGED',
    'CHILD_IDENTITY_CHANGED', 'CHILD_CONTEXT_CHANGED', 'CHILD_OPERATION_CHANGED',
    'CHILD_READBACK_DIGEST_INVALID', 'CHILD_LINEAGE_DIGEST_INVALID',
    'CHILD_ENROLLMENT_CHANGED', 'CHILD_SNAPSHOT_NOT_STABLE',
    'UNOWNED_COMPANY_DISCOVERY_EXISTS', 'CONTAINMENT_OWNERSHIP_CHANGED',
    'CONTAINMENT_LEASE_EXPIRED', 'CONTAINMENT_LEASE_TOO_SHORT',
})
CHILD_FIELDS = ('request_id', 'revision', 'company_id', 'company_source_id', 'profile_id',
    'profile_version', 'source_id', 'parent_operation_id', 'parent_operation_attempt',
    'trigger_job_id', 'candidate_id', 'posting_id', 'snapshot_id', 'snapshot_sha256',
    'quality_observation_id', 'operation_id', 'state', 'authorization', 'reason',
    'operation_attempt', 'operation_sha256', 'pending_sha256')
CHILD_DYNAMIC = {'state', 'authorization', 'reason', 'operation_attempt', 'operation_sha256'}
CHILD_TERMINAL = TERMINAL | {'NEEDS_ORIGIN_REVIEW', 'DISMISSED'}
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
DOMAIN_POLICY_FIELDS = ('id', 'provider', 'tenant', 'strategy', 'capabilities', 'enabled',
                        'request_timeout', 'rate_limit', 'concurrency')
DOMAIN_ENROLLMENT_FIELDS = ('source_id', 'company_source_id', 'operation_id', 'request_id',
    'audit_id', 'audit_sha256', 'pending_sha256', 'receipt_sha256', 'envelope_sha256')
DOMAIN_PROOF_HASH_FIELDS = ('domain_evidence_sha256', 'pending_sha256', 'receipt_sha256',
    'envelope_sha256', 'raw_sha256', 'normalized_sha256', 'source_snapshot_sha256',
    'source_policy_sha256', 'employer_response_sha256', 'audit_sha256', 'sha256')
DOMAIN_UNAVAILABLE_REASONS = {'DISCOVERY_OPERATION_UNAVAILABLE', 'DOMAIN_CORROBORATION_UNAVAILABLE',
    'BOOTSTRAP_RECEIPT_NOT_PERSISTED', 'OFFICIAL_DOMAIN_NOT_ACQUIRED',
    'DOMAIN_ACQUISITION_AUDIT_UNAVAILABLE'}


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


def _validate_lease_continuity(proof, *, prior_expiry, started_at, finished_at, final_expiry):
    """Validate observed overlapping owner checks within this callback only.

    The designated containment owner is the trust boundary, as for the ordinary
    proof. A renewed expiry alone cannot bridge an expired cached proof: every
    completed check must precede the expiry already covered by earlier checks.
    """
    witness = proof.get('lease_continuity')
    _require(isinstance(witness, dict) and set(witness) == {'schema_version', 'checks'}
        and type(witness['schema_version']) is int and witness['schema_version'] == 1
        and isinstance(witness['checks'], list)
        and 1 <= len(witness['checks']) <= LEASE_CONTINUITY_MAX_CHECKS,
        'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID')
    _require(all(isinstance(value, datetime) and value.tzinfo is not None
        and value.utcoffset() == timedelta(0) for value in (started_at, finished_at))
        and finished_at >= started_at, 'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID')
    token_sha = hashlib.sha256(proof['token'].encode('utf-8')).hexdigest()
    covered_until, previous_check = prior_expiry, started_at
    for row in witness['checks']:
        _require(isinstance(row, dict)
            and set(row) == {'owner', 'token_sha256', 'checked_at', 'lease_expires_at'}
            and row['owner'] == proof['owner'] and row['token_sha256'] == token_sha
            and isinstance(row['checked_at'], str) and isinstance(row['lease_expires_at'], str),
            'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID')
        try:
            checked_at, expires = _timestamp(row['checked_at']), _timestamp(row['lease_expires_at'])
        except (ValueError, TypeError, AttributeError):
            raise ObservationError('SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID') from None
        _require(checked_at.utcoffset() == timedelta(0) and expires.utcoffset() == timedelta(0)
            and previous_check <= checked_at <= finished_at,
            'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID')
        _require(expires > checked_at and (covered_until is None or checked_at < covered_until),
            'SOURCE_CONTAINMENT_LEASE_EXPIRED')
        _require(covered_until is None or expires >= covered_until,
            'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID')
        covered_until, previous_check = expires, checked_at
    _require(covered_until == final_expiry, 'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID')
    return deepcopy(witness)


def _evidence_destination(value):
    """Require an absent target in an existing, operator-owned stable directory.

    The coordinator must prevent concurrent directory renames/replacements. We
    reject links/reparse points along the path and recheck its parent identity
    before publishing; the final hard-link creation never replaces a target.
    """
    path = Path(value)
    _require('..' not in path.parts, 'EVIDENCE_PATH_INVALID')
    path = path.absolute()
    for parent in reversed(path.parents):
        info = parent.lstat()
        _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0),
            'EVIDENCE_PARENT_INVALID')
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError('EVIDENCE_TARGET_EXISTS')
    parent_info = path.parent.stat()
    return path, (parent_info.st_dev, parent_info.st_ino)


def _save_evidence(path, parent_identity, evidence):
    _, current_identity = _evidence_destination(path)
    _require(current_identity == parent_identity, 'EVIDENCE_PARENT_CHANGED')
    descriptor, temporary = tempfile.mkstemp(prefix='.public-observation-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        _, current_identity = _evidence_destination(path)
        _require(current_identity == parent_identity, 'EVIDENCE_PARENT_CHANGED')
        # Same-directory hard link is atomic and absent-only, unlike replace().
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


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
    _require(isinstance(rows, list) and len(rows) <= 5, 'PUBLIC_JOB_RESPONSE_INVALID')
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


def child_containment_context(evidence):
    """Project already validated child evidence for callback/release continuity.

    This does not grant source authority; containment independently verifies the
    same read model. An empty completed query remains distinct from no query.
    """
    child = evidence.get('child_discovery') or {}
    requested = child.get('parent_operation_id') is not None
    rows = child.get('children') or []
    if requested:
        _require(child['parent_operation_id'] == evidence.get('collection_operation_id')
            and type(child.get('parent_operation_attempt')) is int
            and child['parent_operation_attempt'] == evidence.get('collection_operation_attempt'),
            'CHILD_DISCOVERY_IDENTITY_CHANGED')
        if child.get('lineage_sha256') is not None:
            identity = [{key: row[key] for key in CHILD_FIELDS if key not in CHILD_DYNAMIC}
                        for row in sorted(rows, key=lambda row: row['request_id'])]
            _require(child['lineage_sha256'] == _digest(identity), 'CHILD_DISCOVERY_IDENTITY_CHANGED')
    else:
        _require(not rows and not child.get('enrolled_sources'), 'CHILD_DISCOVERY_IDENTITY_CHANGED')
    return dict(collection_operation_attempt=evidence.get('collection_operation_attempt'),
        child_discovery_requested=requested,
        owned_child_operation_ids=sorted(row['operation_id'] for row in rows if row['operation_id']),
        owned_child_request_ids=sorted(row['request_id'] for row in rows),
        owned_child_lineage_sha256=child.get('lineage_sha256'),
        owned_child_sources=deepcopy(child.get('enrolled_sources') or []))


def _observe_children(read, evidence, selection, source, *, timeout_seconds, poll_interval, monotonic, sleep):
    """Wait only for the complete, immutable child set of this collection attempt."""
    progress = dict(status='WAITING', reason=None, children=[], enrolled_sources=[], readback_sha256=None, lineage_sha256=None, polls=0,
        acceptance_run_id=evidence['acceptance_run_id'], profile_id=evidence['profile_id'],
        profile_version=selection['profile_version'], source_id=source['id'],
        parent_operation_id=evidence['collection_operation_id'],
        parent_operation_attempt=evidence['collection_operation_attempt'])
    evidence['child_discovery'] = progress
    path = '/profiles/' + quote(evidence['profile_id'], safe='') + '/sources/' + quote(source['id'], safe='')
    path += '/collections/' + quote(evidence['collection_operation_id'], safe='') + '/children'
    deadline, identity, enrolled = monotonic() + timeout_seconds, None, {}
    for _ in range(120):
        result = read('GET', path, params={'attempt': evidence['collection_operation_attempt']})
        _require(isinstance(result, dict) and type(result.get('schema_version')) is int
            and result['schema_version'] == 1 and result.get('complete') is True
            and type(result.get('limit')) is int and result['limit'] == 2
            and result.get('parent_request_id') == evidence['collection_request_id']
            and result.get('sha256') == _digest({key: value for key, value in result.items() if key != 'sha256'}),
            'CHILD_DISCOVERY_LINEAGE_INCOMPLETE')
        for key in ('profile_id', 'profile_version', 'source_id', 'parent_operation_id', 'parent_operation_attempt'):
            _require(type(result.get(key)) is type(progress[key]) and result[key] == progress[key],
                     'CHILD_DISCOVERY_IDENTITY_CHANGED')
        rows = result.get('children')
        _require(isinstance(rows, list) and len(rows) <= 2, 'CHILD_DISCOVERY_LINEAGE_INCOMPLETE')
        for row in rows:
            _require(isinstance(row, dict) and set(row) == set(CHILD_FIELDS), 'CHILD_DISCOVERY_RECORD_INVALID')
            for key in ('profile_id', 'profile_version', 'source_id', 'parent_operation_id', 'parent_operation_attempt'):
                _require(type(row[key]) is type(progress[key]) and row[key] == progress[key],
                         'CHILD_DISCOVERY_IDENTITY_CHANGED')
            for key in ('request_id', 'company_id', 'company_source_id', 'trigger_job_id', 'candidate_id',
                        'posting_id', 'snapshot_id', 'quality_observation_id'):
                _require(isinstance(row[key], str) and SAFE_ID.fullmatch(row[key]), 'CHILD_DISCOVERY_RECORD_INVALID')
            _require(type(row['revision']) is int and row['revision'] >= 1
                and type(row['operation_attempt']) is int and row['operation_attempt'] >= 0
                and row['state'] in CHILD_TERMINAL | {'QUEUED', 'RUNNING', 'RETRY_WAIT'}, 'CHILD_DISCOVERY_RECORD_INVALID')
            for key in ('snapshot_sha256', 'pending_sha256'):
                _require(isinstance(row[key], str) and re.fullmatch(r'[a-f0-9]{64}', row[key]), 'CHILD_DISCOVERY_RECORD_INVALID')
            if row['operation_id'] is not None:
                _require(isinstance(row['operation_id'], str) and SAFE_ID.fullmatch(row['operation_id'])
                    and isinstance(row['operation_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', row['operation_sha256']),
                    'CHILD_DISCOVERY_RECORD_INVALID')
            else:
                _require(row['state'] in {'NEEDS_ORIGIN_REVIEW', 'DISMISSED'} and row['operation_sha256'] is None,
                         'CHILD_DISCOVERY_RECORD_INVALID')
            for key in ('authorization', 'reason'):
                _require(row[key] is None or isinstance(row[key], str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', row[key]),
                         'CHILD_DISCOVERY_RECORD_INVALID')
        ordered = sorted(rows, key=lambda row: row['request_id'])
        _require(len({row['request_id'] for row in rows}) == len(rows)
            and len({row['operation_id'] for row in rows if row['operation_id']}) == sum(bool(row['operation_id']) for row in rows),
            'CHILD_DISCOVERY_RECORD_INVALID')
        current = [{key: row[key] for key in CHILD_FIELDS if key not in CHILD_DYNAMIC} for row in ordered]
        _require(identity is None or current == identity, 'CHILD_DISCOVERY_IDENTITY_CHANGED')
        _require(result.get('lineage_sha256') == _digest(current), 'CHILD_DISCOVERY_IDENTITY_CHANGED')
        source_rows = result.get('enrolled_sources')
        _require(isinstance(source_rows, list) and len(source_rows) <= 2, 'CHILD_DISCOVERY_SOURCE_INVALID')
        current_sources = {}
        for item in source_rows:
            _require(isinstance(item, dict) and set(item) == {'source_id', 'company_source_id', 'operation_id',
                'request_id', 'policy', 'policy_sha256', 'audit_id', 'audit_sha256', 'pending_sha256',
                'receipt_sha256', 'envelope_sha256'}, 'CHILD_DISCOVERY_SOURCE_INVALID')
            matches = [row for row in rows if row['request_id'] == item['request_id']
                and row['operation_id'] == item['operation_id'] and row['pending_sha256'] == item['pending_sha256']]
            _require(len(matches) == 1 and item['source_id'] != source['id']
                and item['source_id'] not in current_sources and isinstance(item['policy'], dict)
                and item['policy'].get('id') == item['source_id'] and item['policy'].get('enabled') is True
                and item['policy_sha256'] == _policy_fingerprint(item['policy']), 'CHILD_DISCOVERY_SOURCE_INVALID')
            for key in ('source_id', 'company_source_id', 'audit_id'):
                _require(isinstance(item[key], str) and SAFE_ID.fullmatch(item[key]), 'CHILD_DISCOVERY_SOURCE_INVALID')
            for key in ('audit_sha256', 'receipt_sha256', 'envelope_sha256'):
                _require(isinstance(item[key], str) and re.fullmatch(r'[a-f0-9]{64}', item[key]), 'CHILD_DISCOVERY_SOURCE_INVALID')
            current_sources[item['source_id']] = item
        _require(all(current_sources.get(key) == value for key, value in enrolled.items()), 'CHILD_DISCOVERY_SOURCE_CHANGED')
        enrolled = current_sources
        if identity is None:
            # The child can come from any of this collection's jobs, not just
            # the at-most-five public cards sampled earlier by this helper.
            for row in ordered:
                job = read('GET', '/jobs/' + quote(row['trigger_job_id'], safe=''),
                           params={'profile_id': evidence['profile_id']})
                _require(job.get('id') == row['trigger_job_id'] and job['id'] != selection['job_id'],
                         'CHILD_DISCOVERY_JOB_CHANGED')
                postings = [posting for posting in job.get('postings', []) if posting.get('id') == row['posting_id']
                    and posting.get('source') == evidence['endpoint'] and posting.get('fetched_at') == source['last_attempt']
                    and _digest(posting.get('raw')) == row['snapshot_sha256']
                    and any(snapshot.get('id') == row['snapshot_id'] and snapshot.get('sha256') == row['snapshot_sha256']
                            for snapshot in posting.get('snapshots', []))]
                _require(len(postings) == 1, 'CHILD_DISCOVERY_SNAPSHOT_CHANGED')
            identity = current
        progress.update(children=ordered, enrolled_sources=source_rows,
                        polls=progress['polls'] + 1, readback_sha256=result['sha256'], lineage_sha256=result['lineage_sha256'])
        if not rows:
            progress.update(status='NOT_RUN', reason='NO_CURRENT_COLLECTION_CHILDREN')
            return
        if all(row['state'] in CHILD_TERMINAL for row in rows):
            progress['status'] = 'COMPLETED' if all(row['state'] == 'COMPLETED' for row in rows) else 'BLOCKED'
            progress['reason'] = None if progress['status'] == 'COMPLETED' else 'CHILD_DISCOVERY_TERMINAL_BLOCK'
            return
        if monotonic() >= deadline:
            break
        sleep(min(poll_interval, max(0., deadline - monotonic())))
    progress.update(status='BLOCKED', reason='CHILD_DISCOVERY_TIMEOUT')


def _validate_domain_proof(proof, child, source, evidence, enrolled):
    fields = set(DOMAIN_PROOF_HASH_FIELDS) | {'company_id', 'request_id', 'request_revision',
        'operation_id', 'domain_id', 'domain', 'domain_verification_method', 'corroboration_method',
        'execution_scope', 'execution_scope_basis', 'native', 'apply_response_sha256',
        'audit_id', 'acquired_at'}
    _require(isinstance(proof, dict) and set(proof) == fields, 'DOMAIN_PROOF_RECORD_INVALID')
    for key in DOMAIN_PROOF_HASH_FIELDS:
        _require(isinstance(proof[key], str) and re.fullmatch(r'[a-f0-9]{64}', proof[key]),
                 'DOMAIN_PROOF_RECORD_INVALID')
    _require(proof['sha256'] == _digest({key: value for key, value in proof.items() if key != 'sha256'}),
             'DOMAIN_PROOF_RECORD_INVALID')
    for key in ('company_id', 'request_id', 'operation_id'):
        _require(isinstance(proof[key], str) and proof[key] == child[key], 'DOMAIN_PROOF_LINEAGE_CHANGED')
    _require(type(proof['request_revision']) is int and proof['request_revision'] == child['revision']
        and proof['pending_sha256'] == child['pending_sha256']
        and proof['raw_sha256'] == child['snapshot_sha256'], 'DOMAIN_PROOF_LINEAGE_CHANGED')
    expected_source = dict(company_id=child['company_id'], provider=source['provider'],
        tenant=source['tenant'], endpoint=evidence['endpoint'], collector_source_id=source['id'])
    # Bootstrap receipts deliberately hash a narrower policy projection than the
    # collector's complete policy fingerprint; never compare the two digests.
    _require(proof['source_snapshot_sha256'] == _digest(expected_source)
        and proof['source_policy_sha256'] == _digest({key: source[key] for key in DOMAIN_POLICY_FIELDS}),
        'DOMAIN_PROOF_LINEAGE_CHANGED')
    for key in ('domain_id', 'audit_id'):
        _require(isinstance(proof[key], str) and SAFE_ID.fullmatch(proof[key]), 'DOMAIN_PROOF_RECORD_INVALID')
    domain = proof['domain']
    _require(isinstance(domain, str) and len(domain) <= 253 and '.' in domain
        and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                for label in domain.split('.')), 'DOMAIN_PROOF_RECORD_INVALID')
    _require(isinstance(proof['domain_verification_method'], str)
        and re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', proof['domain_verification_method'])
        and proof['corroboration_method'] == 'AGGREGATOR_NATIVE_BOOTSTRAP_V1'
        and proof['execution_scope'] in {'CONTROLLED_TRANSPORT', 'PUBLIC_READ_ONLY'}
        and proof['execution_scope_basis'] == 'PERSISTED_RECEIPT_METADATA',
        'DOMAIN_PROOF_RECORD_INVALID')
    _require(isinstance(proof['acquired_at'], str), 'DOMAIN_PROOF_RECORD_INVALID')
    _timestamp(proof['acquired_at'])
    apply_hash = proof['apply_response_sha256']
    _require(apply_hash is None or isinstance(apply_hash, str) and re.fullmatch(r'[a-f0-9]{64}', apply_hash),
             'DOMAIN_PROOF_RECORD_INVALID')
    native = proof['native']
    _require(isinstance(native, dict) and set(native) == {'provider', 'tenant', 'job_id',
        'native_response_sha256', 'native_json_sha256'} and native['provider'] == 'greenhouse',
        'DOMAIN_PROOF_RECORD_INVALID')
    for key in ('tenant', 'job_id'):
        _require(isinstance(native[key], str) and SAFE_ID.fullmatch(native[key]), 'DOMAIN_PROOF_RECORD_INVALID')
    for key in ('native_response_sha256', 'native_json_sha256'):
        _require(isinstance(native[key], str) and re.fullmatch(r'[a-f0-9]{64}', native[key]),
                 'DOMAIN_PROOF_RECORD_INVALID')
    for row in enrolled:
        _require(all(row[key] == proof[key] for key in ('pending_sha256', 'receipt_sha256', 'envelope_sha256')),
                 'DOMAIN_PROOF_LINEAGE_CHANGED')
        full = next(item for item in evidence['child_discovery']['enrolled_sources']
                    if item['source_id'] == row['source_id'])
        _require(full['policy']['provider'] == native['provider'] and full['policy']['tenant'] == native['tenant'],
                 'DOMAIN_PROOF_LINEAGE_CHANGED')


def _observe_domain_proofs(read, evidence, source):
    """Capture one settled DB readback before cleanup, without acceptance credit.

    Receipt execution_scope is preserved metadata, not independent public HTTP
    evidence or a join to the separately selected fictional employer.
    """
    progress = evidence['domain_proof_readback']
    children = evidence['child_discovery']
    if not children['children']:
        progress['reason'] = 'NO_CURRENT_COLLECTION_CHILDREN'
        return
    if not all(row['state'] in CHILD_TERMINAL for row in children['children']):
        progress['reason'] = 'CHILDREN_NOT_SETTLED'
        return
    progress.update(status='FAIL', reason='DOMAIN_PROOF_READBACK_NOT_VALIDATED')
    path = '/profiles/' + quote(evidence['profile_id'], safe='') + '/sources/' + quote(source['id'], safe='')
    path += '/collections/' + quote(evidence['collection_operation_id'], safe='') + '/domain-proofs'
    result = read('GET', path, params={'attempt': evidence['collection_operation_attempt']})
    fields = {'schema_version', 'profile_id', 'profile_version', 'source_id', 'parent_operation_id',
        'parent_operation_attempt', 'parent_request_id', 'complete', 'limit',
        'collection_lineage_sha256', 'collection_readback_sha256', 'observations', 'sha256'}
    _require(isinstance(result, dict) and set(result) == fields
        and type(result['schema_version']) is int and result['schema_version'] == 1
        and result['complete'] is True and type(result['limit']) is int and result['limit'] == 2
        and result['sha256'] == _digest({key: value for key, value in result.items() if key != 'sha256'}),
        'DOMAIN_PROOF_READBACK_INVALID')
    for key in ('profile_id', 'profile_version', 'source_id', 'parent_operation_id', 'parent_operation_attempt'):
        _require(type(result[key]) is type(children[key]) and result[key] == children[key],
                 'DOMAIN_PROOF_LINEAGE_CHANGED')
    _require(result['parent_request_id'] == evidence['collection_request_id']
        and result['collection_lineage_sha256'] == children['lineage_sha256']
        and result['collection_readback_sha256'] == children['readback_sha256'],
        'DOMAIN_PROOF_LINEAGE_CHANGED')
    observations = result['observations']
    _require(isinstance(observations, list) and len(observations) == len(children['children']) <= 2,
             'DOMAIN_PROOF_READBACK_INVALID')
    for row, child in zip(observations, children['children']):
        _require(isinstance(row, dict) and set(row) == {'child', 'status', 'reason', 'proof', 'enrolled_sources'}
            and isinstance(row['child'], dict) and _digest(row['child']) == _digest(child),
            'DOMAIN_PROOF_LINEAGE_CHANGED')
        enrolled = [{key: item[key] for key in DOMAIN_ENROLLMENT_FIELDS}
                    for item in children['enrolled_sources'] if item['request_id'] == child['request_id']]
        _require(isinstance(row['enrolled_sources'], list) and _digest(row['enrolled_sources']) == _digest(enrolled),
                 'DOMAIN_PROOF_LINEAGE_CHANGED')
        if row['status'] == 'UNAVAILABLE':
            _require(row['proof'] is None and row['reason'] in DOMAIN_UNAVAILABLE_REASONS,
                     'DOMAIN_PROOF_RECORD_INVALID')
        else:
            _require(row['status'] == 'AVAILABLE' and row['reason'] is None, 'DOMAIN_PROOF_RECORD_INVALID')
            _validate_domain_proof(row['proof'], child, source, evidence, enrolled)
    progress.update(status='CAPTURED', reason=None, response=deepcopy(result), response_sha256=result['sha256'])


def run(base_url, baseline, evidence_path, *, acceptance_run_id, provider='wwr',
        containment_preflight, request=None, execution_scope='LIVE_PUBLIC_READ_ONLY',
        timeout_seconds=90, child_timeout_seconds=90, poll_interval=.25, monotonic=time.monotonic, sleep=time.sleep,
        utc_now=lambda: datetime.now(timezone.utc)):
    """Return and save a truthful receipt; never retry a collection or submit forms.

    containment_preflight(stage, context) returns scheduler_contained=True,
    source_policy_exclusive=True, source_collection_exclusive=True and stable
    nonempty owner/token strings, plus lease_expires_at (UTC ISO timestamp).
    The operator must verify an actual lease that cannot be released before its
    expiry, covering at least the next 15 seconds. Exclusive collection includes
    manual clients and retry dispatchers. Every contained API read checks the
    lease before and after; an expiry gap or lost proof cannot be reacquired here.
    A long callback may return bounded lease_continuity checkpoints from the
    same owner, proving overlapping renewals through its fresh final proof.
    This cannot rescue a lease already expired before the callback starts.
    Stages include preflight, enable, dispatch, poll, read, finalize and cleanup.
    Context collection IDs are None until the collect POST returns its actual
    operation ID; subsequent callbacks receive that ID and its submitted request ID.
    The caller owns the lease and scheduler lifecycle; this helper never stops
    or starts host services. Injected requests require CONTROLLED_TRANSPORT and
    cannot earn criterion 11 PASS. evidence_path must be absent in an existing,
    operator-owned directory kept stable throughout this run.
    """
    url = urlsplit(base_url)
    if (url.scheme != 'http' or url.hostname not in {'localhost', '127.0.0.1', '::1'}
            or url.username is not None or url.password is not None or url.query or url.fragment
            or url.path not in ('', '/') or provider not in ENDPOINTS
            or execution_scope not in {'LIVE_PUBLIC_READ_ONLY', 'CONTROLLED_TRANSPORT'}
            or not callable(containment_preflight) or not 2 <= timeout_seconds <= 120
            or type(child_timeout_seconds) not in (int, float) or not 2 <= child_timeout_seconds <= 120
            or not .05 <= poll_interval <= 2):
        raise ValueError('PUBLIC_OBSERVATION_CONFIGURATION_INVALID')
    if request is not None and execution_scope != 'CONTROLLED_TRANSPORT':
        raise ValueError('PUBLIC_OBSERVATION_TRANSPORT_SCOPE_INVALID')
    continuity = {key: baseline.get(key) for key in ('profile_id', 'application_id', 'operation_id', 'request_id')}
    if (not all(isinstance(value, str) and value for value in continuity.values())
            or not isinstance(acceptance_run_id, str) or not acceptance_run_id
            or baseline.get('external_submission_enabled') is not False
            or not isinstance(baseline.get('source'), dict) or not baseline['source'].get('commit')):
        raise ValueError('PUBLIC_OBSERVATION_BASELINE_INVALID')
    path, parent_identity = _evidence_destination(evidence_path)
    evidence = dict(schema_version=1, result='FAIL', acceptance_run_id=acceptance_run_id,
        timestamp=utc_now().isoformat(), source=baseline['source'], **continuity,
        execution_scope=execution_scope, evidence_kind='PUBLIC_READ_ONLY', provider=provider,
        endpoint=ENDPOINTS[provider], external_submission_enabled=False,
        observed_real_job_ids=[], observations=[], cleanup={'status': 'NOT_REQUIRED'},
        child_discovery={'status': 'NOT_RUN', 'reason': 'PARENT_COLLECTION_NOT_COMPLETED', 'children': [], 'enrolled_sources': []},
        domain_proof_readback={'status': 'NOT_RUN', 'reason': 'PARENT_COLLECTION_NOT_COMPLETED',
            'basis': 'PERSISTED_RECEIPT_METADATA', 'response': None, 'response_sha256': None},
        inventory_scope='AT_MOST_FIVE_CURRENT_COLLECTION_JOBS',
        limitations=['Observed public jobs are distinct from the selected mock application.',
                    'No real application form or submission is exercised.',
                    'Source policy ownership is operator verified; the API provides no atomic policy CAS.'])
    owned_source = fingerprint = initial_selection = ownership = None
    lease_expiry = operation_id = operation_state = None
    containment_lost = False
    enable_attempted = False
    with (httpx.Client(base_url=base_url, timeout=API_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False)
          if request is None else nullcontext()) as client:
        if request is None:
            def request(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()

        def contained(stage):
            nonlocal ownership, lease_expiry, containment_lost
            try:
                _require(not containment_lost, 'SOURCE_CONTAINMENT_LOST')
                now = utc_now()
                _require(lease_expiry is None or lease_expiry > now, 'SOURCE_CONTAINMENT_LEASE_EXPIRED')
                callback_started_at = now
                required_until = now + timedelta(seconds=API_TIMEOUT_SECONDS)
                proof = containment_preflight(stage, dict(provider=provider, base_url=base_url,
                    acceptance_run_id=acceptance_run_id, source_id=evidence.get('source_id') or owned_source,
                    collection_operation_id=evidence.get('collection_operation_id'),
                    collection_request_id=evidence.get('collection_request_id'),
                    **child_containment_context(evidence),
                    source_policy_sha256=evidence.get('source_policy_sha256'), newly_created=bool(owned_source),
                    lease_required_until=required_until.isoformat()))
                _require(isinstance(proof, dict) and proof.get('scheduler_contained') is True
                    and proof.get('source_policy_exclusive') is True
                    and proof.get('source_collection_exclusive') is True
                    and all(isinstance(proof.get(key), str) and proof[key] for key in ('owner', 'token')),
                    'SCHEDULER_CONTAINMENT_REQUIRED')
                current = (proof['owner'], proof['token'])
                _require(ownership is None or ownership == current, 'SOURCE_OWNERSHIP_CHANGED')
                now = utc_now()
                crossed_expiry = lease_expiry is not None and lease_expiry <= now
                _require(not crossed_expiry or 'lease_continuity' in proof,
                         'SOURCE_CONTAINMENT_LEASE_EXPIRED')
                required_until = now + timedelta(seconds=API_TIMEOUT_SECONDS)
                try:
                    expires = _timestamp(proof['lease_expires_at'])
                    _require(expires.utcoffset() == timedelta(0) and expires > required_until,
                             'SOURCE_CONTAINMENT_LEASE_INVALID')
                except (KeyError, TypeError, AttributeError, ValueError):
                    raise ObservationError('SOURCE_CONTAINMENT_LEASE_INVALID') from None
                if 'lease_continuity' in proof:
                    witness = _validate_lease_continuity(proof, prior_expiry=lease_expiry,
                        started_at=callback_started_at, finished_at=now, final_expiry=expires)
                    if crossed_expiry:
                        records = evidence.setdefault('lease_continuity_checks', [])
                        _require(len(records) < LEASE_CONTINUITY_MAX_RECORDS,
                                 'SOURCE_CONTAINMENT_LEASE_CONTINUITY_LIMIT')
                        records.append(dict(stage=stage, previous_expires_at=lease_expiry.isoformat(),
                            callback_started_at=callback_started_at.isoformat(), checked_at=now.isoformat(),
                            lease_expires_at=expires.isoformat(), witness=witness, sha256=_digest(witness)))
                ownership, lease_expiry = current, expires
            except Exception as error:
                # Imported only at runtime: containment also imports this module.
                # Never persist arbitrary exception text, payloads or credentials.
                from cli.acceptance_containment import ContainmentError
                if (type(error) is ContainmentError and len(error.args) == 1
                        and type(error.args[0]) is str and error.args[0] in SAFE_CONTAINMENT_ERROR_CODES):
                    evidence.setdefault('containment_failure', dict(schema_version=1,
                        stage=stage, error_class='ContainmentError', code=error.args[0]))
                containment_lost = True
                raise
            evidence['containment'] = dict(owner=proof['owner'], token_sha256=_digest(proof['token']),
                scheduler_contained=True, source_policy_exclusive=True, source_collection_exclusive=True,
                lease_expires_at=expires.isoformat(), checked_at=now.isoformat(), last_stage=stage)

        def read(method, path, **kwargs):
            stage = 'poll' if path.startswith('/operations/') else 'read'
            contained(stage)
            response = request(method, path, **kwargs)
            contained(stage)
            return response

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
            source = _source(read, provider)
            if source is None:
                _require(_source(read, provider) is None, 'SOURCE_APPEARED_DURING_PREFLIGHT')
                policy = dict(provider=provider, tenant='default', enabled=True, poll_interval=604800,
                    concurrency=1, request_timeout=10, rate_limit=1, jitter_seconds=0)
                from services.discovery.sources.registry import source_adapter
                capabilities = source_adapter(policy).capabilities()
                contained('enable')
                enable_attempted = True
                configured = request('PUT', '/sources', json=policy)
                _require(isinstance(configured, dict) and isinstance(configured.get('id'), str)
                    and SAFE_ID.fullmatch(configured['id']) is not None, 'SOURCE_CONFIGURATION_CHANGED')
                owned_source = configured['id']
                expected_fingerprint = _policy_fingerprint(dict(id=owned_source, **policy,
                    strategy=capabilities.strategy.value, capabilities=capabilities.as_dict()))
                # This is a requested constraint, not an observed source receipt.
                # The owner independently checks it on both guarded reads; only
                # the exact returned projection can authorize cleanup or dispatch.
                evidence.update(source_id=owned_source, source_policy_sha256=expected_fingerprint)
                source = _source(read, provider)
                _require(source is not None and source['id'] == owned_source
                    and all(source[key] == value for key, value in policy.items())
                    and _policy_fingerprint(source) == expected_fingerprint, 'SOURCE_CONFIGURATION_CHANGED')
                fingerprint = expected_fingerprint
            _require(source['enabled'] is True, 'SOURCE_DISABLED')
            now = utc_now()
            _require(not source.get('backoff_until') or _timestamp(source['backoff_until']) <= now, 'SOURCE_BACKOFF')
            _require(not source.get('last_attempt') or _timestamp(source['last_attempt']) +
                timedelta(seconds=source['poll_interval']) <= now, 'SOURCE_COOLDOWN')
            evidence.update(source_id=source['id'], source_policy_sha256=_policy_fingerprint(source))
            contained('dispatch')
            current = _source(read, provider)
            _require(current is not None and _policy_fingerprint(current) == evidence['source_policy_sha256']
                and current.get('last_attempt') == source.get('last_attempt')
                and current.get('backoff_until') == source.get('backoff_until'), 'SOURCE_CONFIGURATION_CHANGED')
            _require(_selection(read, baseline) == initial_selection, 'BASELINE_SELECTION_CHANGED')
            collection_request_id = 'public-observation-' + uuid4().hex
            contained('dispatch')
            queued = request('POST', '/sources/' + quote(source['id'], safe='') + '/collect',
                json=dict(profile_id=baseline['profile_id'], request_id=collection_request_id))
            operation_id = queued['operation_id']
            evidence.update(collection_operation_id=operation_id, collection_request_id=collection_request_id)
            deadline = monotonic() + timeout_seconds
            while True:
                operation = read('GET', '/operations/' + quote(operation_id, safe=''))
                params = operation.get('params') or {}
                _require(operation.get('id') == operation_id and operation.get('kind') == 'DISCOVER'
                    and operation.get('entity_id') == baseline['profile_id']
                    and params.get('source_id') == source['id'] and params.get('request_id') == collection_request_id
                    and params.get('profile_version') == initial_selection['profile_version'], 'COLLECTION_IDENTITY_CHANGED')
                operation_state = operation['state']
                error = operation.get('error_category') or operation.get('error')
                evidence.update(collection_state=operation['state'], collection_error=(error if
                    isinstance(error, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', error)
                    else 'UNCLASSIFIED_PROVIDER_FAILURE' if error else None))
                evidence['collection_operation_sha256'] = _digest(operation)
                if operation['state'] in TERMINAL:
                    break
                if monotonic() >= deadline:
                    raise ObservationError('PUBLIC_COLLECTION_TIMEOUT')
                sleep(poll_interval)
            _require(operation['state'] == 'COMPLETED', 'PUBLIC_COLLECTION_' + operation['state'])
            _require(operation.get('attempt_count') == 1 and operation.get('retry_count', 0) == 0,
                     'PUBLIC_COLLECTION_RETRIED')
            evidence['collection_operation_attempt'] = operation['attempt_count']
            source = _source(read, provider)
            _require(source is not None and source['id'] == evidence['source_id']
                and source.get('last_success') == source.get('last_attempt'), 'SOURCE_RECEIPT_CHANGED')
            _require(_policy_fingerprint(source) == evidence['source_policy_sha256'], 'SOURCE_CONFIGURATION_CHANGED')
            evidence['source_record_sha256'] = _digest(source)
            _require(_selection(read, baseline) == initial_selection, 'BASELINE_SELECTION_CHANGED')
            evidence['observations'] = _public_jobs(read, source, operation, initial_selection,
                baseline['profile_id'], ENDPOINTS[provider])
            evidence['observed_real_job_ids'] = sorted(row['job_id'] for row in evidence['observations'])
            _observe_children(read, evidence, initial_selection, source, timeout_seconds=child_timeout_seconds,
                              poll_interval=poll_interval, monotonic=monotonic, sleep=sleep)
            _observe_domain_proofs(read, evidence, source)
            _require(read('GET', '/health').get('external_submission_enabled') is False,
                     'EXTERNAL_SUBMISSION_SETTING_CHANGED')
            contained('finalize')
            evidence['result'] = 'PASS'
        except ObservationError as error:
            evidence['error_code'] = str(error)
            if evidence['child_discovery']['status'] == 'WAITING':
                evidence['child_discovery'].update(status='FAIL', reason=str(error))
        except Exception:
            evidence['error_code'] = 'PUBLIC_OBSERVATION_API_OR_RECORD_INVALID'
            if evidence['child_discovery']['status'] == 'WAITING':
                evidence['child_discovery'].update(status='FAIL', reason='CHILD_DISCOVERY_RECORD_INVALID')
        finally:
            if operation_id and operation_state not in TERMINAL:
                # Cancelling this helper's queued operation remains safe even
                # after lease loss. Never retry a cancellation or collection.
                try:
                    cancelled = request('POST', '/operations/' + quote(operation_id, safe='') + '/cancel')
                    evidence['cancellation_requested'] = cancelled.get('cancel_requested') is True
                except Exception:
                    evidence['cancellation_requested'] = False
            if enable_attempted and owned_source is None:
                evidence['cleanup'] = {'status': 'ENABLE_UNCONFIRMED_PRESERVE_CONTAINMENT'}
            if owned_source:
                try:
                    contained('cleanup')
                    current = _source(read, provider)
                    if current is None or fingerprint is None or _policy_fingerprint(current) != fingerprint:
                        evidence['cleanup'] = {'status': 'PRESERVED_POLICY_CHANGED'}
                    else:
                        policy = {key: current[key] for key in POLICY_FIELDS}
                        contained('cleanup')
                        request('PUT', '/sources', json={**policy, 'enabled': False})
                        restored = _source(read, provider)
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
                    _require(_selection(request if containment_lost else read, baseline) == initial_selection,
                             'BASELINE_SELECTION_CHANGED')
                except ObservationError as error:
                    evidence.update(result='FAIL', error_code=str(error))
                except Exception:
                    evidence.update(result='FAIL', error_code='BASELINE_SELECTION_CHANGED')
    accepted = evidence['result'] == 'PASS' and execution_scope == 'LIVE_PUBLIC_READ_ONLY'
    evidence['acceptance_observations'] = [dict(step=11,
        status=('PASS' if accepted else 'NOT_RUN' if execution_scope == 'CONTROLLED_TRANSPORT' else 'FAIL'),
        observable_result=('Current public source jobs observed for the unchanged selected fixture profile/application.'
            if accepted else 'Controlled transport or failed observation does not establish live-source acceptance.'),
        evidence_kind='PUBLIC_READ_ONLY', source_commit=baseline['source']['commit'], run_id=acceptance_run_id,
        continuity=continuity, checks=[dict(name=CHECK, passed=accepted)])]
    _save_evidence(path, parent_identity, evidence)
    return evidence
