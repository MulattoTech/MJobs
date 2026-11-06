"""Deterministic contract tests; fake time is never connected acceptance evidence."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from cli import acceptance_monitoring as acceptance
from cli import local_persistence_acceptance as persistence
from cli.compose_recovery import _inventory_digest
from services.company_discovery.monitoring import source_version


class World:
    def __init__(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.seconds = 0
        self.calls = []
        self.source = dict(id='source1', company_id='company1', provider='mock-ats', tenant='example',
            collection_strategy='FIXTURE', career_url='http://mock:8090/ats/example',
            endpoint='http://mock:8090/ats/example/jobs', collector_source_id=None, enabled=True,
            last_success=self.now().isoformat())
        self.search = dict(id='search1', profile_id='profile1', enabled=True, source_ids=[], criteria={'scheduler': {'enabled': False, 'interval_seconds': 300}})
        self.binding = dict(commit='1'*40, tree='2'*40, working_tree_diff_sha256='3'*64, working_tree_status_sha256='4'*64)
        jobs = [dict(job_id='job1', canonical_sha256='a'*64, source_copy_count=2, source_copies_sha256='b'*64)]
        self.inventory = dict(schema_version=3, profile_id='profile1', job_count=1, source_copy_count=2, jobs=jobs, sha256=_inventory_digest(jobs))
        self.domains = dict(schema_version=1, domains={name: dict(count=1, sha256='c'*64) for name in persistence.DOMAINS})
        self.baseline = dict(source=self.binding, acceptance_run_id='run1', target_origin='http://127.0.0.1:18700',
            profile_id='profile1', application_id='app1', operation_id='app-op', request_id='request1',
            external_submission_enabled=False, confirmation=dict(application_id='app1', receipt='synthetic-receipt', submission_count=1),
            local_persistence=self.domains, job_inventory=self.inventory,
            discovery_search_proofs=[dict(search_profile_id='search1', profile_id='profile1', result='PASS', filtered_titles=['Data Engineer'])])
        self.monitor = None
        self.operations = []
        self.stall = False

    def now(self):
        return self.start + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.seconds += seconds

    def tick(self):
        if self.stall or self.monitor is None or self.now() < datetime.fromisoformat(self.monitor['next_due']):
            return
        due = self.monitor['next_due']
        previous = self.monitor['operation_id']
        identifier = 'refresh' + str(len(self.operations)+1)
        params = dict(fixture_monitoring='direct-company-source-v1', fixture_source_id='source1', profile_id='profile1',
            profile_version='version1', fixture_origin='http://mock:8090', monitoring_revision=1,
            source_version=source_version(self.source), scheduled_for=due, predecessor_operation_id=previous, fetch_mode='FIXTURE')
        self.operations.append(dict(id=identifier, kind='COMPANY_DISCOVER', entity_id='source1', state='COMPLETED',
            attempt_count=1, cancel_requested=False, error=None, params=params, started_at=due, completed_at=self.now().isoformat()))
        self.monitor.update(status='HEALTHY', operation_id=identifier, completed_operation_id=identifier,
            last_attempt=due, next_due=(self.now()+timedelta(seconds=300)).isoformat())
        self.source['last_success'] = self.now().isoformat()

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        self.tick()
        if path == '/settings':
            return dict(mode='LOCAL_FIRST', operation_mode='FIXTURE', external_submission_enabled=False)
        if path == '/profiles/profile1':
            return dict(id='profile1', version='version1')
        if path.startswith('/search-profiles?'):
            return dict(search_profiles=[copy.deepcopy(self.search)])
        if path.startswith('/company-sources/view?'):
            return dict(company_sources=[{**self.source, 'profile_id': 'profile1', 'profile_company_associated': True,
                'source_version': source_version(self.source), 'monitoring': self.monitor}])
        if path == '/company-sources':
            return dict(company_sources=[copy.deepcopy(self.source)])
        if path.endswith('/fixture-monitoring'):
            if method == 'PUT':
                self.monitor = dict(source_id='source1', source_version=source_version(self.source), profile_id='profile1',
                    profile_version='version1', marker='direct-company-source-v1', origin='http://mock:8090',
                    search_fingerprint='d'*64, revision=1, enabled=True, poll_interval=300, status='IDLE',
                    operation_id=None, completed_operation_id=None, next_due=(self.now()+timedelta(seconds=300)).isoformat(),
                    last_attempt=None, failure_count=0, last_error=None, external_submission_enabled=False)
            return copy.deepcopy(self.monitor)
        if path == '/operations/app-op':
            return dict(id='app-op', state='COMPLETED')
        if path.startswith('/operations?'):
            return dict(operations=copy.deepcopy(self.operations))
        if path.startswith('/operations/'):
            return copy.deepcopy(next(row for row in self.operations if row['id'] == path.split('/')[-1]))
        if '/mock-runs/' in path:
            return dict(state='COMPLETED', operation_id='app-op', external_submission_enabled=False)
        if path == '/profiles/profile1/applications/app1':
            return dict(id='app1', profile_id='profile1', state='CONFIRMED', confirmation=self.baseline['confirmation'])
        raise AssertionError((method, path))

    def options(self):
        return dict(request=self.request, source_observer=lambda: self.binding,
            inventory_observer=lambda: copy.deepcopy(self.inventory), persistence_observer=lambda: copy.deepcopy(self.domains),
            now=self.now, monotonic=lambda: self.seconds, sleep=self.sleep, scheduler_tick_seconds=5)


class AcceptanceMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.world = World()
    def tearDown(self):
        self.temp.cleanup()
    def prepare(self, **kwargs):
        return acceptance.prepare('http://127.0.0.1:18700', self.world.baseline, self.root/'prepared.json',
            acceptance_run_id='run1', **{**self.world.options(), **kwargs})
    def restart(self, prepared, **kwargs):
        recovery = dict(scope='CONTROLLED_RECOVERY_SEGMENT', result='PASS', source=self.world.binding, acceptance_run_id='run1',
            recovery_artifact_sha256='e'*64, completed_at_utc=self.world.now().isoformat(),
            **{key: self.world.baseline[key] for key in ('profile_id','application_id','operation_id','request_id')})
        return acceptance.after_restart('http://127.0.0.1:18700', self.world.baseline, prepared, self.root/'restarted.json',
            acceptance_run_id='run1', **{**self.world.options(), 'recovery_evidence': recovery, **kwargs})

    def test_continuous_natural_due_proof_keeps_baseline_and_emits_only_scoped_rows(self):
        original = copy.deepcopy(self.world.baseline)
        prepared = self.prepare()
        self.assertEqual(prepared['result'], 'PARTIAL')
        self.assertEqual(prepared['stage_result'], 'PASS')
        self.assertEqual(prepared['observations'], [])
        self.assertGreaterEqual(self.world.seconds, 300)
        after = self.restart(prepared)
        self.assertEqual(after['result'], 'PARTIAL')
        self.assertEqual([row['step'] for row in after['observations']], [59,60])
        self.assertTrue(all(row['status']=='PASS' for row in after['observations']))
        self.assertGreaterEqual(self.world.seconds, 600)
        self.assertEqual(self.world.baseline, original)
        self.assertEqual([call[0] for call in self.world.calls if call[0]!='GET'], ['PUT'])
        self.assertGreater(len(list(self.root.glob('*.json'))), 2)

    def test_stale_source_or_run_rejected_before_configuration(self):
        for field in ('source', 'acceptance_run_id'):
            with self.subTest(field=field):
                world = World()
                world.baseline[field] = {} if field=='source' else 'other-run'
                with self.assertRaises(acceptance.MonitoringAcceptanceError):
                    acceptance.prepare('http://127.0.0.1:18700', world.baseline, self.root/(field+'.json'), acceptance_run_id='run1', **world.options())
                self.assertFalse(any(method=='PUT' for method, _, _ in world.calls))

    def test_stalled_scheduler_times_out_without_any_manual_enqueue(self):
        self.world.stall = True
        with self.assertRaises(acceptance.MonitoringAcceptanceError):
            self.prepare(timeout_seconds=305)
        self.assertFalse(any(method=='POST' for method, _, _ in self.world.calls))
        self.assertIn('FAIL', (self.root/'prepared.json').read_text())

    def test_existing_evidence_is_preserved_before_any_mutation(self):
        path = self.root/'prepared.json'
        path.write_text('preserve me')
        with self.assertRaises(acceptance.MonitoringAcceptanceError):
            self.prepare()
        self.assertEqual(path.read_text(), 'preserve me')
        self.assertEqual(self.world.calls, [])

    def test_changed_domains_or_due_time_fail_restart_without_pass_rows(self):
        prepared = self.prepare()
        self.world.monitor['next_due'] = (self.world.now()+timedelta(seconds=299)).isoformat()
        with self.assertRaises(acceptance.MonitoringAcceptanceError):
            self.restart(prepared)
        self.assertNotIn('"status": "PASS"', (self.root/'restarted.json').read_text())

    def test_unbound_restart_cannot_receive_observations(self):
        prepared = self.prepare()
        after = self.restart(prepared, recovery_evidence=None)
        self.assertEqual(after['observations'], [])
        self.assertEqual(after['stage_result'], 'PARTIAL')

    def test_restart_operation_or_persistence_drift_cannot_receive_credit(self):
        prepared = self.prepare()
        self.world.operations[0]['attempt_count'] = 2
        with self.assertRaisesRegex(acceptance.MonitoringAcceptanceError, 'RESTART_OPERATION_CHANGED'):
            self.restart(prepared)
        self.assertNotIn('"status": "PASS"', (self.root/'restarted.json').read_text())

    def test_changed_protected_domain_rejected_before_waiting(self):
        prepared = self.prepare()
        self.world.domains = copy.deepcopy(self.world.domains)
        self.world.domains['domains']['generated_resumes']['sha256'] = 'f'*64
        before = self.world.seconds
        with self.assertRaises(acceptance.MonitoringAcceptanceError):
            self.restart(prepared)
        self.assertEqual(self.world.seconds, before)

    def test_duplicate_scheduled_operation_rejected(self):
        prepared = self.prepare()
        original = self.world.tick
        def duplicate():
            original()
            if len(self.world.operations) == 2:
                self.world.operations.append({**self.world.operations[-1], 'id': 'duplicate'})
        self.world.tick = duplicate
        with self.assertRaisesRegex(acceptance.MonitoringAcceptanceError, 'DUPLICATE_OR_UNKNOWN'):
            self.restart(prepared)

    def test_manual_or_retried_new_refresh_rejected(self):
        prepared = self.prepare()
        original = self.world.tick
        def retry():
            original()
            if len(self.world.operations) == 2:
                self.world.operations[-1]['attempt_count'] = 2
        self.world.tick = retry
        with self.assertRaisesRegex(acceptance.MonitoringAcceptanceError, 'SINGLE_ATTEMPT'):
            self.restart(prepared)

    def test_due_time_that_passed_during_recovery_is_not_credited(self):
        prepared = self.prepare()
        self.world.sleep(300)
        with self.assertRaisesRegex(acceptance.MonitoringAcceptanceError, 'RECOVERY_WINDOW_MISSED'):
            self.restart(prepared)

    def test_actual_inventory_reader_preserves_201st_row_overflow_refusal(self):
        original = self.world.request
        def overflow(method, path, **kwargs):
            if path.startswith('/jobs?'):
                self.assertIn('limit=201', path)
                return dict(jobs=[{'id': str(n)} for n in range(201)])
            return original(method, path, **kwargs)
        with self.assertRaises(acceptance.MonitoringAcceptanceError):
            self.prepare(request=overflow, inventory_observer=None)
        self.assertFalse(any(method=='PUT' for method, _, _ in self.world.calls))

    def test_callback_stop_interrupts_wait_and_preserves_failure_checkpoint(self):
        self.world.stall = True
        def stop():
            if self.world.seconds >= 10:
                raise RuntimeError('synthetic requested stop')
        with self.assertRaises(acceptance.MonitoringAcceptanceError):
            self.prepare(check=stop)
        self.assertLessEqual(self.world.seconds, 10)
        self.assertGreater(len(list(self.root.glob('*.json'))), 1)

    def test_manually_dispatched_operation_cannot_count_as_scheduler_resume(self):
        prepared = self.prepare()
        original = self.world.tick
        def manual():
            original()
            if len(self.world.operations) == 2:
                self.world.operations[-1]['params']['manual'] = True
        self.world.tick = manual
        with self.assertRaisesRegex(acceptance.MonitoringAcceptanceError, 'NOT_NATURALLY_BOUND'):
            self.restart(prepared)

    def test_saturated_operation_listing_refuses_completeness_claim(self):
        self.world.operations = [dict(id=str(n), params={}, entity_id='unrelated') for n in range(100)]
        with self.assertRaisesRegex(acceptance.MonitoringAcceptanceError, 'MAY_BE_TRUNCATED'):
            self.prepare()
        self.assertFalse(any(method=='PUT' for method, _, _ in self.world.calls))

    def test_early_queued_operation_is_rejected_before_worker_completion(self):
        prepared = self.prepare()
        original = self.world.request
        def early(method, path, **kwargs):
            if path == '/operations/early':
                raise AssertionError('Early admission must be rejected before operation read')
            value = original(method, path, **kwargs)
            if path.endswith('/fixture-monitoring') and self.world.seconds > 307:
                value.update(operation_id='early', status='SCHEDULED')
            return value
        with self.assertRaisesRegex(acceptance.MonitoringAcceptanceError, 'ADMITTED_BEFORE_DUE'):
            self.restart(prepared, request=early)

    def test_immediately_due_review_survives_response_latency(self):
        self.world.source['last_success'] = (self.world.start-timedelta(seconds=600)).isoformat()
        original = self.world.request
        def delayed(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if method == 'PUT':
                self.world.monitor['next_due'] = self.world.now().isoformat()
                value = copy.deepcopy(self.world.monitor)
                self.world.sleep(2)
            return value
        prepared = self.prepare(request=delayed)
        self.assertEqual(prepared['stage_result'], 'PASS')


if __name__ == '__main__':
    unittest.main()
