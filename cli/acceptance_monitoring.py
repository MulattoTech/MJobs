"""Observe naturally due direct monitoring in one controlled, continuous journey.

Only the reviewed monitoring PUT mutates product state. No manual dispatch,
database access, clock changes, fixture replacement, or overall acceptance PASS
is provided. Observer/time seams are for deterministic tests and bounded runner
integration; defaults use real HTTP, real clocks and existing inventory readers.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote, urlsplit

import httpx

from cli import local_persistence_acceptance as persistence
from cli.compose_recovery import (collect_public_job_inventory, validate_public_job_inventory,
    validate_baseline, verify_recovered_application, compare_public_job_inventory,
    RECOVERY_RESTART_SERVICES, validate_project_name)
from services.company_discovery.monitoring import source_version


ROOT = Path(__file__).resolve().parents[1]
MARKER = 'direct-company-source-v1'
IDS = ('profile_id', 'application_id', 'operation_id', 'request_id')
SOURCE_FIELDS = ('id', 'company_id', 'provider', 'tenant', 'collection_strategy',
                 'career_url', 'endpoint', 'collector_source_id', 'enabled')
MONITOR_FIELDS = ('source_id', 'source_version', 'profile_id', 'profile_version', 'marker',
    'origin', 'search_fingerprint', 'revision', 'enabled', 'poll_interval', 'status',
    'operation_id', 'completed_operation_id', 'next_due', 'last_attempt', 'failure_count',
    'last_error', 'external_submission_enabled')
CHECKS = {
    59: ('restart_scheduler_natural_operation_observed', 'scheduler_admitted_at_persisted_due',
         'scheduler_same_profile_source', 'scheduler_no_manual_enqueue', 'scheduler_single_operation'),
    60: ('natural_due_time_reached', 'same_profile_direct_source_refresh_completed',
         'refresh_attempt_count_one', 'direct_source_identity_preserved', 'refresh_receipt_pointer_advanced',
         'next_due_advanced', 'no_duplicate_refresh', 'stable_seven_domains_and_v3_inventory'),
}


class MonitoringAcceptanceError(RuntimeError):
    pass


def _require(condition, code):
    if not condition:
        raise MonitoringAcceptanceError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _instant(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        _require(stamp.tzinfo is not None, 'MONITOR_EVIDENCE_TIME_INVALID')
        return stamp.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise MonitoringAcceptanceError('MONITOR_EVIDENCE_TIME_INVALID') from None


def _live_source():
    def git(*args):
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True,
            timeout=5, check=True).stdout.strip()
    return dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'),
        working_tree_diff_sha256=hashlib.sha256(git('diff', '--binary', 'HEAD').encode()).hexdigest(),
        working_tree_status_sha256=hashlib.sha256(git('status', '--porcelain=v1', '--untracked-files=all').encode()).hexdigest())


class _Session:
    def __init__(self, base_url, baseline, evidence_path, run_id, *, request=None,
                 get_bytes=None, source_observer=None, inventory_observer=None, persistence_observer=None,
                 now=None, monotonic=None, sleep=None, check=None, timeout_seconds=900,
                 scheduler_tick_seconds=30, persistence_root=ROOT):
        self.path = Path(evidence_path)
        _require(not self.path.exists(), 'MONITOR_EVIDENCE_ALREADY_EXISTS')
        parsed = urlsplit(base_url)
        _require(parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'}
            and parsed.username is None and parsed.password is None and parsed.path in ('', '/')
            and not parsed.query and not parsed.fragment, 'MONITOR_TARGET_NOT_LOOPBACK')
        _require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
            and 1 <= timeout_seconds <= 1800, 'MONITOR_TIMEOUT_INVALID')
        _require(type(scheduler_tick_seconds) is int and 5 <= scheduler_tick_seconds <= 120,
            'MONITOR_TICK_BOUND_INVALID')
        self.base_url, self.baseline, self.run_id = base_url.rstrip('/'), baseline, run_id
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.monotonic, self.sleep, self.check = monotonic or time.monotonic, sleep or time.sleep, check or (lambda: None)
        self.deadline = self.monotonic() + timeout_seconds
        self.tick_seconds = scheduler_tick_seconds
        self.source_observer = source_observer or _live_source
        self.inventory_observer = inventory_observer
        self.persistence_observer = persistence_observer
        self.persistence_root = persistence_root
        self.api, self.bytes_reader = request, get_bytes
        self.client = None
        self.sequence = 0

    def __enter__(self):
        if self.api is None:
            self.client = httpx.Client(base_url=self.base_url, follow_redirects=False, trust_env=False)
        return self

    def __exit__(self, *_):
        if self.client is not None:
            self.client.close()

    def budget(self):
        self.check()
        _require(self.monotonic() < self.deadline, 'MONITOR_OBSERVATION_DEADLINE')

    def pause(self, seconds=1):
        self.budget()
        self.sleep(min(seconds, self.deadline-self.monotonic()))
        self.budget()

    def raw(self, method, path, **kwargs):
        self.budget()
        with self.client.stream(method, path, timeout=min(5, self.deadline-self.monotonic()), **kwargs) as response:
            response.raise_for_status()
            _require(not response.is_redirect, 'MONITOR_API_REDIRECT_REJECTED')
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                self.budget()
                size += len(chunk)
                _require(size <= 8*1024*1024, 'MONITOR_API_RESPONSE_TOO_LARGE')
                chunks.append(chunk)
            return b''.join(chunks)

    def request(self, method, path, **kwargs):
        self.budget()
        value = self.api(method, path, **kwargs) if self.api else json.loads(self.raw(method, path, **kwargs))
        self.budget()
        return value

    def get(self, path):
        return self.request('GET', path)

    def read_bytes(self, path):
        self.budget()
        value = self.bytes_reader(path) if self.bytes_reader else self.raw('GET', path)
        self.budget()
        return value

    def preflight(self):
        validate_baseline(self.baseline)
        source = self.baseline.get('source')
        _require(isinstance(source, dict) and re.fullmatch('[a-f0-9]{40}', source.get('commit', ''))
            and source == self.source_observer(), 'MONITOR_SOURCE_BINDING_CHANGED')
        _require(isinstance(self.run_id, str) and re.fullmatch('[A-Za-z0-9_.:-]{1,128}', self.run_id)
            and self.baseline.get('acceptance_run_id', self.baseline.get('run_id')) == self.run_id,
            'MONITOR_RUN_BINDING_CHANGED')
        _require(self.baseline.get('target_origin') == self.base_url, 'MONITOR_UI_TARGET_CHANGED')
        settings = self.get('/settings')
        _require(settings.get('operation_mode') == 'FIXTURE' and settings.get('external_submission_enabled') is False,
            'MONITOR_FIXTURE_MODE_REQUIRED')
        validate_public_job_inventory(self.baseline.get('job_inventory'))
        persistence.validate(self.baseline.get('local_persistence'))

    def envelope(self, stage):
        return dict(schema_version=1, result='PARTIAL', stage=stage, stage_result='RUNNING',
            timestamp=self.now().isoformat(), source=self.baseline['source'], acceptance_run_id=self.run_id,
            **{key: self.baseline[key] for key in IDS}, baseline_sha256=_digest(self.baseline),
            external_submission_enabled=False, observations=[],
            scope='Controlled same-journey fixture monitoring only; no whole-product acceptance claim')

    def write(self, evidence, phase=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = self.path
        if phase:
            self.sequence += 1
            stamp = self.now().strftime('%Y%m%dT%H%M%S%fZ')
            target = target.with_name(f'{target.stem}.{stamp}.{self.sequence}.{phase}.json')
        value = {**evidence, 'timestamp': self.now().isoformat()}
        with target.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')


def _source(session, source_id):
    rows = session.get('/company-sources')['company_sources']
    matches = [row for row in rows if row.get('id') == source_id]
    _require(len(matches) == 1, 'MONITOR_SOURCE_IDENTITY_AMBIGUOUS')
    return matches[0]


def _snapshot(source):
    return {key: source[key] for key in SOURCE_FIELDS}


def _search(session, search_id):
    rows = session.get('/search-profiles?profile_id=' + quote(session.baseline['profile_id'], safe=''))['search_profiles']
    matches = [row for row in rows if row.get('id') == search_id]
    _require(len(matches) == 1 and matches[0].get('profile_id') == session.baseline['profile_id']
        and matches[0].get('enabled') is True and matches[0].get('source_ids') == [], 'MONITOR_SEARCH_BINDING_CHANGED')
    return matches[0]


def _select(session, source_id, search_profile_id):
    profile_id = session.baseline['profile_id']
    proofs = [row for row in session.baseline.get('discovery_search_proofs', [])
        if row.get('profile_id') == profile_id and row.get('result') == 'PASS' and row.get('filtered_titles')]
    _require(proofs, 'MONITOR_UI_SEARCH_PROOF_REQUIRED')
    search_id = search_profile_id or proofs[-1]['search_profile_id']
    _require(any(row['search_profile_id'] == search_id for row in proofs), 'MONITOR_SEARCH_NOT_FROM_UI_BASELINE')
    search = _search(session, search_id)
    rows = session.get('/company-sources/view?profile_id=' + quote(profile_id, safe=''))['company_sources']
    eligible = [row for row in rows if row.get('profile_id') == profile_id and row.get('profile_company_associated') is True
        and row.get('provider') == 'mock-ats' and row.get('collection_strategy') == 'FIXTURE' and row.get('enabled') is True
        and (source_id is None or row.get('id') == source_id)]
    _require(len(eligible) == 1, 'MONITOR_DIRECT_SOURCE_SELECTION_AMBIGUOUS')
    view = eligible[0]
    source = _source(session, view['id'])
    _require(source_version(source) == view['source_version'], 'MONITOR_SOURCE_VIEW_VERSION_CHANGED')
    endpoint = urlsplit(source['endpoint'])
    _require(endpoint.scheme == 'http' and endpoint.hostname in {'127.0.0.1', 'localhost', 'mock'}
        and endpoint.username is None and endpoint.password is None and not endpoint.query and not endpoint.fragment
        and endpoint.path == '/ats/' + source['tenant'] + '/jobs', 'MONITOR_SOURCE_NOT_DIRECT_FIXTURE')
    profile = session.get('/profiles/' + quote(profile_id, safe=''))
    _require(profile.get('id') == profile_id and isinstance(profile.get('version'), str), 'MONITOR_PROFILE_CHANGED')
    return dict(source_snapshot=_snapshot(source), source_version=view['source_version'],
        source_last_success=source['last_success'], source_id=source['id'], profile_version=profile['version'],
        origin=endpoint.scheme+'://'+endpoint.netloc, search_profile_id=search_id, search_sha256=_digest(search),
        revision=(view.get('monitoring') or {}).get('revision', 0))


def _observe(session, context):
    _require(session.source_observer() == session.baseline['source'], 'MONITOR_SOURCE_BINDING_CHANGED')
    _require(_snapshot(_source(session, context['source_id'])) == context['source_snapshot'], 'MONITOR_DIRECT_SOURCE_CHANGED')
    _require(_digest(_search(session, context['search_profile_id'])) == context['search_sha256'], 'MONITOR_SEARCH_BINDING_CHANGED')
    profile = session.get('/profiles/' + quote(session.baseline['profile_id'], safe=''))
    _require(profile.get('id') == session.baseline['profile_id'] and profile.get('version') == context['profile_version'], 'MONITOR_PROFILE_CHANGED')
    verify_recovered_application(session.request, session.baseline)
    inventory = (session.inventory_observer() if session.inventory_observer else
        collect_public_job_inventory(session.get, session.baseline['profile_id']))
    validate_public_job_inventory(inventory)
    compare_public_job_inventory(session.baseline, inventory, inventory)
    domains = (session.persistence_observer() if session.persistence_observer else
        persistence.collect(session.get, session.read_bytes, session.baseline['profile_id'], session.baseline['application_id'], root=session.persistence_root))
    persistence.compare_baseline(session.baseline, domains)
    return dict(job_inventory=inventory, local_persistence=domains)


def _monitor_path(session, context):
    return '/profiles/' + quote(session.baseline['profile_id'], safe='') + '/company-sources/' + quote(context['source_id'], safe='') + '/fixture-monitoring'


def _monitor(session, context, expected):
    row = session.get(_monitor_path(session, context))
    _require(row.get('enabled') is True and row.get('external_submission_enabled') is False
        and type(row.get('revision')) is int and type(row.get('poll_interval')) is int,
        'MONITOR_REVIEWED_CONFIGURATION_CHANGED')
    for key in ('source_id', 'source_version', 'profile_id', 'profile_version', 'marker', 'origin',
                'search_fingerprint', 'revision', 'enabled', 'poll_interval', 'external_submission_enabled'):
        _require(row.get(key) == expected.get(key), 'MONITOR_REVIEWED_CONFIGURATION_CHANGED')
    _require(row.get('status') not in {'FAILED', 'BLOCKED'}, 'MONITOR_REFRESH_BLOCKED')
    return row


def _operations(session, context, expected_ids):
    rows = session.get('/operations?profile_id=' + quote(session.baseline['profile_id'], safe=''))['operations']
    _require(len(rows) < 100, 'MONITOR_OPERATION_HISTORY_MAY_BE_TRUNCATED')
    selected = [row for row in rows if row.get('params', {}).get('fixture_source_id') == context['source_id']
        or row.get('entity_id') == context['source_id']]
    _require(len(selected) == len(expected_ids) and {row['id'] for row in selected} == set(expected_ids), 'MONITOR_DUPLICATE_OR_UNKNOWN_OPERATION')


def _wait_refresh(session, context, expected, previous_id):
    due = _instant(expected['next_due'])
    while True:
        session.budget()
        row = _monitor(session, context, expected)
        identifier = row.get('operation_id')
        if identifier and identifier != previous_id:
            _require(session.now() >= due, 'MONITOR_OPERATION_ADMITTED_BEFORE_DUE')
            operation = session.get('/operations/' + quote(identifier, safe=''))
            params = dict(fixture_monitoring=MARKER, fixture_source_id=context['source_id'],
                profile_id=session.baseline['profile_id'], profile_version=context['profile_version'],
                fixture_origin=expected['origin'], monitoring_revision=expected['revision'], source_version=context['source_version'],
                scheduled_for=expected['next_due'], predecessor_operation_id=previous_id, fetch_mode='FIXTURE')
            _require(operation.get('id') == identifier and operation.get('kind') == 'COMPANY_DISCOVER'
                and operation.get('entity_id') == context['source_id'] and operation.get('params') == params,
                'MONITOR_OPERATION_NOT_NATURALLY_BOUND')
            _require(operation.get('state') in {'QUEUED', 'RUNNING', 'COMPLETED'}, 'MONITOR_OPERATION_FAILED')
            if operation['state'] == 'COMPLETED':
                _require(session.now() >= due and _instant(operation['started_at']) >= due
                    and _instant(operation['completed_at']) >= _instant(operation['started_at']), 'MONITOR_OPERATION_BEFORE_DUE')
                _require(type(operation.get('attempt_count')) is int and operation['attempt_count'] == 1
                    and not operation.get('cancel_requested') and operation.get('error') is None, 'MONITOR_OPERATION_NOT_SINGLE_ATTEMPT')
                _require(row['status'] == 'HEALTHY' and row['completed_operation_id'] == identifier, 'MONITOR_RECEIPT_POINTER_MISMATCH')
                source = _source(session, context['source_id'])
                _require(_instant(row['next_due']) > due and _instant(source['last_success']) >= due
                    and _instant(row['next_due']) == _instant(source['last_success']) + timedelta(seconds=300), 'MONITOR_NEXT_DUE_NOT_ADVANCED')
                known = ([previous_id] if previous_id else []) + [identifier]
                _operations(session, context, known)
                until = session.monotonic()+session.tick_seconds+1
                while session.monotonic() < until:
                    session.pause(min(1, until-session.monotonic()))
                _require(_monitor(session, context, row) == row, 'MONITOR_DUPLICATE_AFTER_SCHEDULER_TICK')
                _operations(session, context, known)
                operation = {key: operation[key] for key in ('id','kind','entity_id','state','attempt_count','params','started_at','completed_at')}
                return row, operation
        else:
            _require(row.get('next_due') == expected['next_due'], 'MONITOR_PERSISTED_DUE_CHANGED')
        session.pause()


def _run(stage, base_url, baseline, evidence_path, acceptance_run_id, action, options):
    with _Session(base_url, baseline, evidence_path, acceptance_run_id, **options) as session:
        evidence = None
        try:
            session.preflight()
            evidence = session.envelope(stage)
            action(session, evidence)
            evidence['timestamp'] = session.now().isoformat()
            session.write(evidence)
            return evidence
        except Exception as error:
            if evidence is not None:
                evidence.update(stage_result='FAIL', observations=[], failure_code=(str(error) if isinstance(error, MonitoringAcceptanceError) else type(error).__name__))
                session.write(evidence)
            if isinstance(error, MonitoringAcceptanceError):
                raise
            raise MonitoringAcceptanceError('MONITOR_OBSERVATION_FAILED:' + type(error).__name__) from None


def prepare(base_url, baseline, evidence_path, *, acceptance_run_id, source_id=None, search_profile_id=None, **options):
    def action(session, evidence):
        context = _select(session, source_id, search_profile_id)
        _observe(session, context)
        _operations(session, context, [])
        review_started = session.now()
        configured = session.request('PUT', _monitor_path(session, context), json=dict(
            source_version=context['source_version'], revision=context['revision'], poll_interval=300))
        _require(configured.get('source_id') == context['source_id'] and configured.get('source_version') == context['source_version']
            and configured.get('profile_id') == baseline['profile_id'] and configured.get('profile_version') == context['profile_version']
            and configured.get('marker') == MARKER and configured.get('revision') == context['revision']+1
            and configured.get('origin') == context['origin']
            and re.fullmatch('[a-f0-9]{64}', configured.get('search_fingerprint', ''))
            and configured.get('poll_interval') == 300 and configured.get('enabled') is True
            and configured.get('external_submission_enabled') is False and configured.get('status') == 'IDLE'
            and configured.get('operation_id') is None and configured.get('completed_operation_id') is None,
            'MONITOR_REVIEW_RESPONSE_INVALID')
        _require(_instant(configured['next_due']) >= review_started
            and _instant(configured['next_due']) >= _instant(context['source_last_success'])+timedelta(seconds=300), 'MONITOR_REVIEW_SHORTENED_INTERVAL')
        evidence.update(monitoring_context=context, initial_due=configured['next_due'])
        session.write(evidence, 'reviewed-waiting-natural-due')
        row, operation = _wait_refresh(session, context, configured, None)
        evidence.update(_observe(session, context), monitoring_binding={key: row[key] for key in MONITOR_FIELDS},
            refresh_operation=operation, refresh_receipt=dict(kind='PUBLIC_COMPLETED_OPERATION_POINTER',
                source_id=context['source_id'], profile_id=baseline['profile_id'], operation_id=row['completed_operation_id']), stage_result='PASS')
        session.write(evidence, 'first-natural-refresh-completed')
    return _run('prepare', base_url, baseline, evidence_path, acceptance_run_id, action, options)


def _validate_recovery_artifact(receipt, baseline, base_url):
    """Bind trusted coordinator metadata to bounded, preserved raw recovery bytes.

    The legacy raw schema has only application identity; the coordinator binds
    source/run/remaining identities. This checks its actual restart evidence,
    not authenticity against a malicious local administrator.
    """
    path_value = receipt.get('recovery_artifact_path')
    _require(isinstance(path_value, str) and Path(path_value).is_absolute(), 'MONITOR_RECOVERY_ARTIFACT_PATH_REQUIRED')
    try:
        with Path(path_value).open('rb') as stream:
            payload = stream.read(8*1024*1024+1)
        _require(len(payload) <= 8*1024*1024 and hashlib.sha256(payload).hexdigest() == receipt.get('recovery_artifact_sha256'),
            'MONITOR_RECOVERY_ARTIFACT_HASH_MISMATCH')
        raw = json.loads(payload)
        _require(isinstance(raw, dict) and raw.get('result') == 'PASS'
            and raw.get('external_submission_enabled') is False and raw.get('application_id') == baseline['application_id']
            and raw.get('timestamp') == receipt.get('completed_at_utc'), 'MONITOR_RECOVERY_ARTIFACT_RESULT_INVALID')
        # Future raw schemas may include the coordinator bindings directly.
        for key in ('source', 'acceptance_run_id', *IDS):
            if key in raw:
                _require(raw[key] == receipt.get(key), 'MONITOR_RECOVERY_ARTIFACT_IDENTITY_CHANGED')
        target = raw.get('compose_target')
        _require(isinstance(target, dict) and target == receipt.get('compose_target')
            and target.get('profile') == 'fixture' and type(target.get('port')) is int
            and target['port'] == urlsplit(base_url).port
            and target.get('services') == list(RECOVERY_RESTART_SERVICES), 'MONITOR_RECOVERY_ARTIFACT_TARGET_INVALID')
        validate_project_name(target.get('project_name'))
        health = {name: dict(status='running', health='healthy') for name in RECOVERY_RESTART_SERVICES}
        _require(raw.get('container_health', {}).get('before') == health
            and raw.get('container_health', {}).get('after') == health, 'MONITOR_RECOVERY_ARTIFACT_HEALTH_INVALID')
        _require(any(row.get('action') == 'All services restarted; original application, operation and independent receipt survived without another submission'
            and row.get('result') == 'PASS' for row in raw.get('actions', [])), 'MONITOR_RECOVERY_ARTIFACT_RESTART_MISSING')
        domains = raw.get('local_persistence', {})
        inventory = raw.get('public_job_inventory_recovery', {})
        _require(domains.get('result') == 'PASS' and sorted(domains.get('domains', [])) == sorted(persistence.DOMAINS)
            and inventory.get('result') == 'PASS' and inventory.get('sha256') == baseline['job_inventory']['sha256'],
            'MONITOR_RECOVERY_ARTIFACT_PERSISTENCE_INVALID')
        return raw
    except MonitoringAcceptanceError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise MonitoringAcceptanceError('MONITOR_RECOVERY_ARTIFACT_INVALID') from None


def after_restart(base_url, baseline, preparation, evidence_path, *, acceptance_run_id, recovery_evidence=None, **options):
    def action(session, evidence):
        _require(preparation.get('stage') == 'prepare' and preparation.get('stage_result') == 'PASS'
            and preparation.get('baseline_sha256') == _digest(baseline) and preparation.get('source') == baseline['source']
            and preparation.get('acceptance_run_id') == acceptance_run_id
            and all(preparation.get(key) == baseline[key] for key in IDS), 'MONITOR_PREPARATION_BINDING_CHANGED')
        context, expected = preparation['monitoring_context'], preparation['monitoring_binding']
        if recovery_evidence is not None:
            _require(recovery_evidence.get('scope') == 'CONTROLLED_RECOVERY_SEGMENT' and recovery_evidence.get('result') == 'PASS'
                and recovery_evidence.get('source') == baseline['source'] and recovery_evidence.get('acceptance_run_id') == acceptance_run_id
                and all(recovery_evidence.get(key) == baseline[key] for key in IDS)
                and re.fullmatch('[a-f0-9]{64}', recovery_evidence.get('recovery_artifact_sha256', '')),
                'MONITOR_RECOVERY_BINDING_CHANGED')
            _validate_recovery_artifact(recovery_evidence, baseline, session.base_url)
            completed = _instant(recovery_evidence['completed_at_utc'])
            _require(_instant(preparation['timestamp']) <= completed <= session.now()
                and completed < _instant(expected['next_due']), 'MONITOR_RECOVERY_WINDOW_MISSED')
            evidence['recovery_evidence_sha256'] = _digest(recovery_evidence)
        _require(session.now() < _instant(expected['next_due']), 'MONITOR_RESTART_WINDOW_MISSED')
        current = _monitor(session, context, expected)
        _require({key: current[key] for key in MONITOR_FIELDS} == expected, 'MONITOR_RESTART_PERSISTENCE_CHANGED')
        prior_operation = session.get('/operations/' + quote(expected['operation_id'], safe=''))
        _require({key: prior_operation.get(key) for key in preparation['refresh_operation']} == preparation['refresh_operation']
            and not prior_operation.get('cancel_requested') and prior_operation.get('error') is None,
            'MONITOR_RESTART_OPERATION_CHANGED')
        _operations(session, context, [expected['operation_id']])
        evidence.update(preparation_sha256=_digest(preparation), monitoring_context=context, pre_due_restart_inventory=_observe(session, context))
        session.write(evidence, 'restart-persistence-before-natural-due')
        row, operation = _wait_refresh(session, context, expected, expected['operation_id'])
        evidence.update(_observe(session, context), monitoring_binding={key: row[key] for key in MONITOR_FIELDS},
            refresh_operation=operation, refresh_receipt=dict(kind='PUBLIC_COMPLETED_OPERATION_POINTER',
                source_id=context['source_id'], profile_id=baseline['profile_id'], operation_id=row['completed_operation_id']),
            stage_result='PASS' if recovery_evidence is not None else 'PARTIAL')
        if recovery_evidence is not None:
            _validate_recovery_artifact(recovery_evidence, baseline, session.base_url)
            evidence['observations'] = [dict(step=step, status='PASS', evidence_kind='CONTROLLED_FIXTURE',
                observable_result=('Scheduler admitted the persisted naturally due same-source task after the bound restart'
                    if step == 59 else 'Same-profile direct company monitoring completed once after restart; inventory and seven persistence domains remained stable'),
                source_commit=baseline['source']['commit'], run_id=acceptance_run_id,
                continuity={key: baseline[key] for key in IDS}, checks=[dict(name=name, passed=True) for name in names])
                for step, names in CHECKS.items()]
        else:
            evidence['limitations'] = ['No bound recovery artifact; steps 59 and 60 withheld']
    return _run('after_restart', base_url, baseline, evidence_path, acceptance_run_id, action, options)
