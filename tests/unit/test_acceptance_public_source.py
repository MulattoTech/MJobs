"""No-network contracts for the same-profile public observation helper."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cli.acceptance_public_source import run, _digest


STAMP = '2026-11-06T12:00:00+00:00'
VERSION = '1' * 64
BASELINE = dict(source={'commit': 'a' * 40}, profile_id='profile', application_id='app',
    operation_id='original-operation', request_id='original-request', external_submission_enabled=False,
    selected_application_job_id='fixture-job', selected_application_route_id='fixture-route')
PROOF = dict(scheduler_contained=True, source_policy_exclusive=True, source_collection_exclusive=True,
    owner='test-operator', token='test-lease', lease_expires_at='2026-11-06T12:05:00+00:00')


class ControlledAPI:
    def __init__(self):
        self.calls = []
        self.source = None
        self.state = 'COMPLETED'
        self.after_collection = lambda: None
        self.children = []
        self.children_complete = True
        self.child_polls = 0
        self.after_child_poll = lambda: None
        self.profile = dict(id='profile', version=VERSION)
        self.app = dict(id='app', profile_id='profile', job_id='fixture-job', route_id='fixture-route',
            target='MOCK', state='CONFIRMED', confirmation={'receipt': 'mock-only'})
        self.fixture = dict(id='fixture-job', attributes={'fictional': True}, routes=[
            dict(id='fixture-route', job_id='fixture-job', provider='mock-ats', url='http://127.0.0.1:1/apply')])
        raw = {'id': 'provider-job', 'title': 'Public Python role'}
        checksum = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
        self.public = dict(id='real-job', attributes=dict(fictional=False, provider='wwr',
            source_id='public-source', source_endpoint='https://weworkremotely.com/remote-jobs.rss',
            native_posting_id='provider-job', submission_capability='MANUAL_ONLY'),
            match=dict(profile_id='profile', profile_version=VERSION), postings=[dict(
                id='public-posting', source='https://weworkremotely.com/remote-jobs.rss',
                fetched_at=STAMP, raw=raw, snapshots=[dict(id='snapshot', sha256=checksum, retrieved_at=STAMP)])])

    def configured(self, enabled=True):
        self.source = dict(id='public-source', provider='wwr', tenant='default', enabled=enabled,
            poll_interval=604800, concurrency=1, request_timeout=10, rate_limit=1, jitter_seconds=0,
            strategy='FEED', capabilities={'direct_application': False}, last_attempt=None,
            last_success=None, backoff_until=None, health_status='NOT_CHECKED', jobs_seen=0)
        return self.source

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if (method, path) in [('GET', '/settings'), ('GET', '/health')]:
            return dict(operation_mode='FIXTURE', external_submission_enabled=False)
        if (method, path) == ('GET', '/profiles/profile'):
            return deepcopy(self.profile)
        if (method, path) == ('GET', '/profiles/profile/applications/app'):
            return deepcopy(self.app)
        if (method, path) == ('GET', '/jobs/fixture-job'):
            return deepcopy(self.fixture)
        if (method, path) == ('GET', '/sources'):
            return {'sources': [deepcopy(self.source)] if self.source else []}
        if (method, path) == ('PUT', '/sources'):
            if self.source is None:
                self.configured()
            self.source.update(kwargs['json'])
            return {'id': 'public-source'}
        if (method, path) == ('POST', '/sources/public-source/collect'):
            self.params = dict(kwargs['json'], source_id='public-source', profile_version=VERSION)
            self.source.update(last_attempt=STAMP, last_success=STAMP, jobs_seen=1, health_status='HEALTHY')
            self.after_collection()
            return {'operation_id': 'public-operation'}
        if (method, path) == ('GET', '/operations/public-operation'):
            return dict(id='public-operation', kind='DISCOVER', entity_id='profile', params=self.params,
                state=self.state, error='SOURCE_UNAVAILABLE' if self.state == 'BLOCKED' else None,
                attempt_count=1, retry_count=0, started_at=STAMP, completed_at=STAMP)
        if (method, path) == ('GET', '/profiles/profile/sources/public-source/collections/public-operation/children'):
            self.child_polls += 1
            self.after_child_poll()
            value = dict(schema_version=1, profile_id='profile', profile_version=VERSION,
                source_id='public-source', parent_operation_id='public-operation', parent_operation_attempt=1,
                parent_request_id=self.params['request_id'], complete=self.children_complete, limit=2,
                children=deepcopy(self.children), enrolled_sources=[])
            value['lineage_sha256'] = _digest([{key: val for key, val in row.items()
                if key not in {'state', 'authorization', 'reason', 'operation_attempt', 'operation_sha256'}}
                for row in sorted(value['children'], key=lambda item: item['request_id'])])
            value['sha256'] = _digest(value)
            return value
        if (method, path) == ('POST', '/operations/public-operation/cancel'):
            return {'cancel_requested': True}
        if (method, path) == ('GET', '/jobs'):
            return {'jobs': [deepcopy(self.public)]}
        if (method, path) == ('GET', '/jobs/real-job'):
            return deepcopy(self.public)
        raise AssertionError((method, path, kwargs))


class PublicSourceObservationTests(unittest.TestCase):
    def setUp(self):
        self.api = ControlledAPI()
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / 'public.json'
        self.observation_count = 0

    def observe(self, **kwargs):
        self.observation_count += 1
        path = self.path if self.observation_count == 1 else self.path.with_name(
            'public-' + str(self.observation_count) + '.json')
        return run('http://127.0.0.1:18700', BASELINE, path,
            acceptance_run_id='campaign', request=self.api.request, execution_scope='CONTROLLED_TRANSPORT',
            containment_preflight=kwargs.pop('containment_preflight', lambda *_: deepcopy(PROOF)),
            utc_now=lambda: datetime.fromisoformat(STAMP), **kwargs)

    def test_one_collection_records_distinct_ids_and_restores_new_source(self):
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['observed_real_job_ids'], ['real-job'])
        self.assertEqual(result['selected_application_job_id'], 'fixture-job')
        self.assertEqual(result['operation_id'], 'original-operation')
        self.assertEqual(result['collection_operation_id'], 'public-operation')
        self.assertEqual(len(result['collection_operation_sha256']), 64)
        self.assertEqual(len(result['source_record_sha256']), 64)
        self.assertEqual(result['acceptance_observations'][0]['status'], 'NOT_RUN')
        self.assertEqual(result['cleanup']['status'], 'RESTORED_DISABLED')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(json.loads(self.path.read_text()), result)
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)

    def child(self, state='RUNNING'):
        return dict(request_id='child-request', revision=2, company_id='company', company_source_id='company-source',
            profile_id='profile', profile_version=VERSION, source_id='public-source',
            parent_operation_id='public-operation', parent_operation_attempt=1, trigger_job_id='real-job',
            candidate_id='candidate', posting_id='public-posting', snapshot_id='snapshot',
            snapshot_sha256=self.api.public['postings'][0]['snapshots'][0]['sha256'],
            quality_observation_id='quality', operation_id='child-operation', state=state,
            authorization='AGGREGATOR_BOOTSTRAP_PENDING', reason=None, operation_attempt=1,
            operation_sha256='a' * 64, pending_sha256='b' * 64)

    def test_exact_child_finishes_while_source_and_lease_remain_active(self):
        self.api.children = [self.child()]
        def advance():
            self.assertTrue(self.api.source['enabled'])
            if self.api.child_polls == 2:
                self.api.children[0]['state'] = 'COMPLETED'
        self.api.after_child_poll = advance
        result = self.observe(sleep=lambda _: None)
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['child_discovery']['status'], 'COMPLETED')
        self.assertEqual(self.api.child_polls, 2)
        self.assertFalse(self.api.source['enabled'])
        self.assertFalse(any(path == '/operations' for _, path, _ in self.api.calls))

    def test_empty_complete_child_set_is_not_run(self):
        contexts = []
        def contain(stage, context):
            contexts.append((stage, deepcopy(context)))
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=contain)
        self.assertEqual(result['child_discovery']['status'], 'NOT_RUN')
        self.assertEqual(result['child_discovery']['children'], [])
        final_contexts = [context for stage, context in contexts if stage in {'finalize', 'cleanup'}]
        self.assertTrue(final_contexts)
        self.assertTrue(all(context['child_discovery_requested'] is True for context in final_contexts))
        self.assertTrue(all(context['owned_child_operation_ids'] == [] for context in final_contexts))
        self.assertTrue(all(context['owned_child_lineage_sha256'] == _digest([]) for context in final_contexts))

    def test_blocked_child_is_explicit_and_does_not_gain_acceptance_credit(self):
        self.api.children = [self.child('BLOCKED')]
        self.api.children[0]['reason'] = 'ROBOTS_DENIED'
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['child_discovery']['status'], 'BLOCKED')
        self.assertEqual(result['child_discovery']['children'][0]['reason'], 'ROBOTS_DENIED')
        self.assertEqual([row['step'] for row in result['acceptance_observations']], [11])

    def test_child_timeout_is_bounded_and_cleanup_revokes_pending_source(self):
        self.api.children = [self.child()]
        ticks = iter(range(100))
        result = self.observe(child_timeout_seconds=2, monotonic=lambda: next(ticks), sleep=lambda _: None)
        self.assertEqual(result['child_discovery']['status'], 'BLOCKED')
        self.assertEqual(result['child_discovery']['reason'], 'CHILD_DISCOVERY_TIMEOUT')
        self.assertFalse(self.api.source['enabled'])

    def test_child_lineage_or_incomplete_response_is_never_admitted(self):
        for change in ('parent', 'profile', 'source', 'snapshot', 'incomplete', 'replacement'):
            with self.subTest(change=change):
                self.api = ControlledAPI()
                self.api.children = [self.child()]
                row = self.api.children[0]
                if change == 'parent': row['parent_operation_id'] = 'other'
                if change == 'profile': row['profile_version'] = 'c' * 64
                if change == 'source': row['source_id'] = 'other'
                if change == 'snapshot': row['snapshot_sha256'] = 'c' * 64
                if change == 'incomplete': self.api.children_complete = False
                if change == 'replacement':
                    def replace():
                        if self.api.child_polls == 2:
                            self.api.children[0]['operation_id'] = 'unrelated'
                    self.api.after_child_poll = replace
                result = self.observe(sleep=lambda _: None)
                self.assertEqual(result['result'], 'FAIL', result)
                self.assertNotEqual(result['child_discovery']['status'], 'COMPLETED')
                self.assertFalse(self.api.source['enabled'])

    def test_injected_request_cannot_claim_live_scope_even_by_default(self):
        for options in ({}, {'execution_scope': 'LIVE_PUBLIC_READ_ONLY'}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'TRANSPORT_SCOPE'):
                run('http://127.0.0.1:18700', BASELINE, self.path, acceptance_run_id='campaign',
                    request=self.api.request, containment_preflight=lambda *_: deepcopy(PROOF), **options)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.path.exists())

    def test_containment_context_uses_only_dispatched_collection_identity(self):
        contexts = []
        def containment(stage, context):
            dispatched = [kwargs['json'] for method, path, kwargs in self.api.calls
                if method == 'POST' and path.endswith('/collect')]
            contexts.append((stage, deepcopy(context), deepcopy(dispatched)))
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'PASS', result)
        self.assertTrue(any(not dispatched for _, _, dispatched in contexts))
        self.assertTrue(any(dispatched for _, _, dispatched in contexts))
        for _stage, context, dispatched in contexts:
            self.assertIn('collection_operation_id', context)
            self.assertIn('collection_request_id', context)
            self.assertEqual(context['collection_operation_id'], 'public-operation' if dispatched else None)
            self.assertEqual(context['collection_request_id'], dispatched[0]['request_id'] if dispatched else None)
        self.assertNotEqual(result['collection_operation_id'], BASELINE['operation_id'])
        self.assertNotEqual(result['collection_request_id'], BASELINE['request_id'])

    def test_unconfirmed_dispatch_does_not_invent_callback_identity(self):
        original = self.api.request
        contexts = []
        def failed_dispatch(method, path, **kwargs):
            if method == 'POST' and path.endswith('/collect'):
                raise RuntimeError('No confirmed response')
            return original(method, path, **kwargs)
        self.api.request = failed_dispatch
        def containment(_stage, context):
            contexts.append(deepcopy(context))
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'FAIL')
        self.assertTrue(contexts)
        self.assertTrue(all('collection_operation_id' in row and row['collection_operation_id'] is None
            and 'collection_request_id' in row and row['collection_request_id'] is None for row in contexts))

    def test_existing_evidence_is_preserved_before_any_request(self):
        self.path.write_text('prior receipt', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.observe()
        self.assertEqual(self.path.read_text(), 'prior receipt')
        self.assertEqual(self.api.calls, [])

    def test_symlink_evidence_or_parent_is_rejected_before_any_request(self):
        for parent_link in (False, True):
            with self.subTest(parent_link=parent_link):
                target = Path(self.directory) / ('target-directory' if parent_link else 'missing.json')
                link = Path(self.directory) / ('linked-directory' if parent_link else 'linked.json')
                if parent_link:
                    target.mkdir()
                try:
                    link.symlink_to(target, target_is_directory=parent_link)
                except OSError as error:
                    self.skipTest('This Windows account cannot create symlinks: ' + str(error.winerror))
                self.path = link / 'receipt.json' if parent_link else link
                with self.assertRaises((ValueError, FileExistsError)):
                    self.observe()
                self.assertTrue(link.is_symlink())
                self.assertEqual(self.api.calls, [])

    def test_late_evidence_collision_is_preserved_after_source_cleanup(self):
        self.api.after_collection = lambda: self.path.write_text('another receipt', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.observe()
        self.assertEqual(self.path.read_text(), 'another receipt')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    def test_atomic_publication_cannot_overwrite_a_last_instant_collision(self):
        original = os.link
        def raced(source, destination):
            Path(destination).write_text('concurrent receipt', encoding='utf-8')
            return original(source, destination)
        with patch('cli.acceptance_public_source.os.link', side_effect=raced), self.assertRaises(FileExistsError):
            self.observe()
        self.assertEqual(self.path.read_text(), 'concurrent receipt')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction contract')
    def test_junction_parent_is_rejected_before_any_request(self):
        target = Path(self.directory) / 'target-directory'
        target.mkdir()
        junction = Path(self.directory) / 'junction-directory'
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
            capture_output=True, text=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.path = junction / 'public.json'
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_PARENT_INVALID'):
            self.observe()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(list(target.iterdir()), [])

    def test_absent_expired_or_short_lease_blocks_before_enable(self):
        for expiry in (None, '2026-11-06T11:59:59+00:00', '2026-11-06T12:00:10+00:00'):
            with self.subTest(expiry=expiry):
                self.api = ControlledAPI()
                proof = dict(PROOF, lease_expires_at=expiry)
                result = self.observe(containment_preflight=lambda *_: proof)
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_INVALID')
                self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_containment_is_checked_between_polls_and_loss_cancels_once(self):
        self.api.state = 'RUNNING'
        heartbeat = {'lost': False}
        clock = [0.0]
        def sleep(_seconds):
            heartbeat['lost'] = True
            clock[0] += _seconds
        result = self.observe(sleep=sleep, timeout_seconds=2, monotonic=lambda: clock[0],
            containment_preflight=lambda *_: dict(PROOF,
            source_collection_exclusive=not heartbeat['lost']))
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertTrue(result['cancellation_requested'])
        self.assertEqual(sum(path.endswith('/cancel') for _, path, _ in self.api.calls), 1)
        self.assertEqual(sum(path == '/operations/public-operation' for _, path, _ in self.api.calls), 1)
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')

    def test_lease_gap_cannot_be_hidden_by_renewal_after_a_poll(self):
        now = [datetime.fromisoformat(STAMP)]
        original = self.api.request
        def delayed(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if path == '/operations/public-operation':
                now[0] = datetime.fromisoformat('2026-11-06T12:06:00+00:00')
            return value
        self.api.request = delayed
        result = run('http://127.0.0.1:18700', BASELINE, self.path, acceptance_run_id='campaign',
            request=self.api.request, execution_scope='CONTROLLED_TRANSPORT', utc_now=lambda: now[0],
            containment_preflight=lambda *_: dict(PROOF, lease_expires_at=(
                '2026-11-06T12:10:00+00:00' if now[0].minute == 6 else PROOF['lease_expires_at'])))
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_EXPIRED')
        self.assertFalse(any(path == '/jobs/real-job' for _, path, _ in self.api.calls))

    def test_lost_containment_at_public_list_prevents_detail_reads(self):
        original = self.api.request
        lost = [False]
        def listed(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if path == '/jobs':
                lost[0] = True
            return value
        self.api.request = listed
        result = self.observe(containment_preflight=lambda *_: dict(PROOF, scheduler_contained=not lost[0]))
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertFalse(any(path == '/jobs/real-job' for _, path, _ in self.api.calls))

    def test_public_rows_must_be_a_list_of_at_most_five_before_detail_reads(self):
        for rows in ([{'id': 'real-job'}] * 6, {'id': 'real-job'}):
            with self.subTest(rows=rows):
                self.api = ControlledAPI()
                original = self.api.request
                def oversized(method, path, **kwargs):
                    value = original(method, path, **kwargs)
                    return {'jobs': rows} if path == '/jobs' else value
                self.api.request = oversized
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'PUBLIC_JOB_RESPONSE_INVALID')
                self.assertFalse(any(path == '/jobs/real-job' for _, path, _ in self.api.calls))

    def test_exactly_five_public_jobs_are_allowed(self):
        original = self.api.request
        jobs = {f'real-job-{number}': dict(deepcopy(self.api.public), id=f'real-job-{number}')
                for number in range(5)}
        def five(method, path, **kwargs):
            if method == 'GET' and path.removeprefix('/jobs/') in jobs:
                return jobs[path.removeprefix('/jobs/')]
            value = original(method, path, **kwargs)
            return {'jobs': list(jobs.values())} if path == '/jobs' else value
        self.api.request = five
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['observed_real_job_ids'], sorted(jobs))

    def test_missing_containment_blocks_before_any_write(self):
        result = self.observe(containment_preflight=lambda *_: {'scheduler_contained': False})
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_scheduler_pause_without_exclusive_collection_owner_is_insufficient(self):
        result = self.observe(containment_preflight=lambda *_: dict(PROOF, source_collection_exclusive=False))
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_policy_change_at_dispatch_cannot_start_collection(self):
        self.api.configured()
        def containment(stage, _context):
            if stage == 'dispatch':
                self.api.source['request_timeout'] = 12
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['error_code'], 'SOURCE_CONFIGURATION_CHANGED')
        self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_ambiguous_enable_response_requires_containment_to_remain_held(self):
        original = self.api.request
        def ambiguous(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if method == 'PUT':
                raise RuntimeError('Connection ended after remote configuration committed')
            return value
        self.api.request = ambiguous
        result = self.observe()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['cleanup']['status'], 'ENABLE_UNCONFIRMED_PRESERVE_CONTAINMENT')
        self.assertTrue(self.api.source['enabled'])
        self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_disabled_source_is_not_reenabled_or_reconfigured(self):
        self.api.configured(enabled=False)
        original = deepcopy(self.api.source)
        result = self.observe()
        self.assertEqual(result['error_code'], 'SOURCE_DISABLED')
        self.assertEqual(self.api.source, original)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_existing_enabled_policy_is_used_without_configuration_writes(self):
        self.api.configured()
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertTrue(self.api.source['enabled'])
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.api.calls))

    def test_dispatch_preflight_receives_exact_existing_source_identity_and_policy(self):
        self.api.configured()
        def containment(stage, context):
            if stage == 'dispatch':
                self.assertEqual(context['source_id'], 'public-source')
                self.assertEqual(len(context['source_policy_sha256']), 64)
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'PASS', result)

    def test_cooldown_or_backoff_prevents_collection(self):
        for field in ('last_attempt', 'backoff_until'):
            with self.subTest(field=field):
                self.api = ControlledAPI()
                self.api.configured()[field] = ('2026-11-07T12:00:00+00:00'
                    if field == 'backoff_until' else STAMP)
                result = self.observe()
                self.assertIn(result['error_code'], {'SOURCE_COOLDOWN', 'SOURCE_BACKOFF'})
                self.assertFalse(any(method != 'GET' for method, _, _ in self.api.calls))

    def test_terminal_provider_failure_is_recorded_without_retry(self):
        self.api.state = 'BLOCKED'
        result = self.observe()
        self.assertEqual(result['error_code'], 'PUBLIC_COLLECTION_BLOCKED')
        self.assertEqual(result['collection_error'], 'SOURCE_UNAVAILABLE')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)

    def test_cancelled_operation_remains_cancelled_without_retry(self):
        self.api.state = 'CANCELLED'
        result = self.observe()
        self.assertEqual(result['error_code'], 'PUBLIC_COLLECTION_CANCELLED')
        self.assertEqual(result['collection_state'], 'CANCELLED')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)

    def test_foreign_operation_and_retried_result_cannot_pass(self):
        for change in ('params', 'attempts'):
            with self.subTest(change=change):
                self.api = ControlledAPI()
                original = self.api.request
                def changed(method, path, **kwargs):
                    value = original(method, path, **kwargs)
                    if method == 'GET' and path == '/operations/public-operation':
                        if change == 'params':
                            value['params'] = dict(value['params'], source_id='foreign-source')
                        else:
                            value['attempt_count'] = 2
                    return value
                self.api.request = changed
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertIn(result['error_code'], {'COLLECTION_IDENTITY_CHANGED', 'PUBLIC_COLLECTION_RETRIED'})

    def test_timeout_requests_cancellation_and_disables_owned_source(self):
        self.api.state = 'RUNNING'
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        result = self.observe(timeout_seconds=2, monotonic=lambda: clock[0], sleep=sleep)
        self.assertEqual(result['error_code'], 'PUBLIC_COLLECTION_TIMEOUT')
        self.assertTrue(result['cancellation_requested'])
        self.assertFalse(self.api.source['enabled'])

    def test_changed_source_policy_is_preserved_during_cleanup(self):
        self.api.after_collection = lambda: self.api.source.update(rate_limit=7)
        result = self.observe()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_POLICY_CHANGED')
        self.assertEqual(self.api.source['rate_limit'], 7)
        self.assertTrue(self.api.source['enabled'])

    def test_existing_source_disablement_during_observation_is_preserved_and_rejected(self):
        self.api.configured()
        self.api.after_collection = lambda: self.api.source.update(enabled=False)
        result = self.observe()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['error_code'], 'SOURCE_CONFIGURATION_CHANGED')
        self.assertFalse(self.api.source['enabled'])
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.api.calls))

    def test_lost_ownership_preserves_source_and_fails_cleanup(self):
        def containment(stage, _context):
            return dict(PROOF, token='other-owner' if stage == 'cleanup' else PROOF['token'])
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
        self.assertTrue(self.api.source['enabled'])

    def test_selected_job_or_profile_changes_cannot_pass(self):
        for change in ('job', 'profile'):
            with self.subTest(change=change):
                self.api = ControlledAPI()
                self.api.after_collection = (lambda: self.api.app.update(job_id='real-job')) if change == 'job' else (
                    lambda: self.api.profile.update(version='2' * 64))
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'BASELINE_SELECTION_CHANGED')

    def test_snapshot_corruption_and_fictional_jobs_cannot_prove_real_collection(self):
        for field in ('snapshot', 'fictional', 'stale-fetch'):
            with self.subTest(field=field):
                self.api = ControlledAPI()
                if field == 'snapshot':
                    self.api.public['postings'][0]['snapshots'][0]['sha256'] = '0' * 64
                elif field == 'fictional':
                    self.api.public['attributes']['fictional'] = True
                else:
                    self.api.public['postings'][0]['fetched_at'] = '2026-11-05T12:00:00+00:00'
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'PUBLIC_JOB_PROVENANCE_INVALID')

    def test_helper_does_not_publish_unstructured_provider_diagnostics(self):
        self.api.state = 'BLOCKED'
        original = self.api.request
        def private_failure(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if method == 'GET' and path == '/operations/public-operation':
                value['error'] = 'PRIVATE_SENTINEL token=value applicant@example.test'
            return value
        self.api.request = private_failure
        result = self.observe()
        self.assertEqual(result['collection_error'], 'UNCLASSIFIED_PROVIDER_FAILURE')
        self.assertNotIn('PRIVATE_SENTINEL', self.path.read_text())

    def test_unsupported_provider_and_nonloopback_api_are_rejected_before_requests(self):
        with self.assertRaises(ValueError):
            self.observe(provider='remoteok')
        self.assertEqual(self.api.calls, [])
        with self.assertRaises(ValueError):
            run('https://external.example', BASELINE, self.path, acceptance_run_id='campaign',
                containment_preflight=lambda *_: deepcopy(PROOF), request=self.api.request)
        self.assertEqual(self.api.calls, [])
