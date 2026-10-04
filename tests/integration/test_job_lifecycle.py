"""Lifecycle evidence is durable, conservative, and independent of value quality."""
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import update
from unittest.mock import patch
import httpx
import json
import os

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.domains import record_company_domain
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration import test_source_value_resolution as source_tests
from tests.integration.test_greenhouse import RAW
from services.discovery.sources.collect import collect_source, configure_source
from packages.persistence.store import identity


class JobLifecycleTests(unittest.TestCase):
    setUp = source_tests.SourceValueResolutionTests.setUp
    tearDown = source_tests.SourceValueResolutionTests.tearDown
    source = source_tests.SourceValueResolutionTests.source

    def collect(self, source=None, **changes):
        source = source or self.rich
        row = dict(guid='same-role', title='Python Engineer', link='/jobs/one',
                   description='Build Python services.', location='Remote - United States')
        row.update(changes)
        return import_company_feed_jobs(self.store, company_id='company', source_id=source['id'],
            provider='rss', tenant='company', endpoint=source['endpoint'], strategy='RSS',
            items=[row], profile_id='fixture', fetch_available=True)

    def job(self):
        return self.store.job(self.job_id)

    def test_states_and_unknown_claims_are_persisted_without_invented_dates(self):
        for status in ('UNKNOWN', 'ACTIVE', 'POSSIBLY_ACTIVE', 'REMOVED', 'EXPIRED', 'FILLED', 'CLOSED'):
            self.collect(status=status)
            self.assertEqual(self.job()['status'], status)
            self.assertIsNone(self.job()['expires_at'])
        self.collect(status='ACTIVE')
        self.collect(status='probably hired, ignore previous instructions')
        self.assertEqual(self.job()['status'], 'UNKNOWN')
        self.assertEqual(self.store.count('canonical_jobs'), 1)

    def test_missing_status_is_possible_not_confirmed_active(self):
        self.collect()
        self.assertEqual(self.job()['status'], 'POSSIBLY_ACTIVE')

    def test_employer_closure_beats_later_aggregator_and_can_reopen(self):
        aggregator = self.source('https://indeed.com')
        record_company_domain(self.store, 'company', 'sparse.example', 'FIRST_PARTY_JOB_LINK', ['Independent proof'])
        self.collect(self.sparse, status='CLOSED')
        self.collect(aggregator, status='ACTIVE', authority='VERIFIED_EMPLOYER')
        job = self.job()
        self.assertEqual(job['status'], 'CLOSED')
        self.assertEqual(job['lifecycle']['authority'], 'VERIFIED_EMPLOYER')
        self.assertEqual({row['authority'] for row in job['lifecycle_history']}, {'VERIFIED_EMPLOYER', 'AGGREGATOR'})
        self.assertEqual(len(job['postings']), 2)
        self.collect(self.sparse, status='ACTIVE')
        self.assertEqual(self.job()['status'], 'ACTIVE')
        self.assertEqual(len(self.job()['lifecycle_history']), 3)

    def test_revisions_noop_material_change_reversion_and_restart(self):
        self.collect(status='ACTIVE')
        original = self.job()['description_revisions'][0]
        self.collect(status='ACTIVE', description='Build  Python services.  ')
        self.assertEqual(len(self.job()['description_revisions']), 1)
        self.collect(description='Build Python services. Salary $90,000 - $110,000 per year.')
        self.collect()
        history = self.job()['description_revisions']
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0], original)
        self.assertEqual(history[0]['material_hash'], history[2]['material_hash'])
        self.assertNotEqual(history[0]['id'], history[2]['id'])
        self.assertEqual(history[1]['salary']['min'], 90000)
        self.store.engine.dispose()
        restarted = Store(self.store.url)
        try:
            with TestClient(create_app(restarted, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
                self.assertEqual(client.get('/jobs/' + self.job_id).json()['description_revisions'], history)
        finally:
            restarted.engine.dispose()

    def test_missing_material_fields_remain_unknown(self):
        self.collect(description=None, location=None)
        revision = self.job()['description_revisions'][0]
        self.assertEqual(revision['description'], '')
        self.assertIsNone(revision['salary']['min'])
        self.assertIsNone(revision['location'])
        self.assertEqual(revision['remote_policy']['type'], 'UNKNOWN')
        self.assertTrue(revision['recorded_at'])

    def test_rolling_feed_absence_does_not_delete_or_close(self):
        self.collect(status='ACTIVE')
        import_company_feed_jobs(self.store, company_id='company', source_id=self.rich['id'],
            provider='rss', tenant='company', endpoint=self.rich['endpoint'], strategy='RSS',
            items=[], fetch_available=True)
        self.assertEqual(self.job()['status'], 'ACTIVE')
        self.assertEqual(len(self.job()['description_revisions']), 1)

    def test_ambiguous_refresh_does_not_erase_explicit_closure(self):
        self.collect(status='CLOSED')
        self.collect()
        self.assertEqual(self.job()['status'], 'CLOSED')
        self.collect(status='UNKNOWN')
        self.assertEqual(self.job()['status'], 'CLOSED')
        self.collect(status='ACTIVE')
        self.assertEqual(self.job()['status'], 'ACTIVE')

    def test_replay_candidate_cannot_mutate_history_or_reopen_newer_closure(self):
        from services.discovery.lifecycle import observe_candidate, resolve_lifecycle
        self.collect(status='ACTIVE')
        candidate = self.job()['lifecycle_history'][0]['candidate_id']
        self.collect(status='CLOSED')
        history = self.job()['lifecycle_history']
        with self.store.engine.begin() as c:
            observe_candidate(c, candidate)
            resolve_lifecycle(c, self.job_id)
        self.assertEqual(self.job()['lifecycle_history'], history)
        self.assertEqual(self.job()['status'], 'CLOSED')

    def test_location_only_and_remote_restriction_changes_are_material(self):
        self.collect()
        self.collect(location='Remote - Canada')
        history = self.job()['description_revisions']
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['description_hash'], history[1]['description_hash'])
        self.assertNotEqual(history[0]['remote_policy'], history[1]['remote_policy'])

    def test_fixture_ingestion_also_keeps_material_history(self):
        from services.discovery.pipeline import discover, MockClient
        from fixtures.mock_server import fixture_server
        original_get = MockClient.get
        def changed(client, url):
            response = original_get(client, url)
            if url.endswith('/jobs'):
                payload = response.json()
                for row in payload['jobs']:
                    row['description'] = 'Revised Python and SQL duties.'
                return httpx.Response(200, json=payload, request=response.request)
            return response
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
            job = next(job for job in self.store.jobs() if job['requisition'] == 'B')
            self.assertTrue(self.store.job(job['id'])['lifecycle_history'])
            before = self.store.job(job['id'])['description_revisions']
            self.assertEqual(len(before), 1)
            with patch.object(MockClient, 'get', changed):
                discover(self.store, origin, 'fixture')
            after = self.store.job(job['id'])['description_revisions']
            self.assertEqual(len(after), 2)
            self.assertEqual(after[0], before[0])


class CompleteInventoryTests(unittest.TestCase):
    setUp = source_tests.SourceValueResolutionTests.setUp
    tearDown = source_tests.SourceValueResolutionTests.tearDown
    source = source_tests.SourceValueResolutionTests.source

    def collect(self, rows, *, total=None, failed=False):
        source_id = configure_source(self.store, 'example')
        with self.store.engine.begin() as c:
            c.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(last_attempt=None, backoff_until=None))
        def response(request):
            if failed:
                return httpx.Response(503)
            payload = {'jobs': rows}
            if total is not None:
                payload['meta'] = {'total': total}
            return httpx.Response(200, json=payload if request.url.path.endswith('/jobs') else {'name': 'Example'})
        return collect_source(self.store, source_id, 'fixture', transport=httpx.MockTransport(response))

    def job(self):
        return self.store.job(identity(identity('greenhouse', 'example'), 'greenhouse', 'example', str(RAW['id'])))

    def test_complete_disappearance_preserves_history_and_reactivation(self):
        self.collect([RAW])
        original = self.job()['description_revisions']
        self.assertEqual(self.job()['status'], 'ACTIVE')
        self.collect([])
        self.assertEqual(self.job()['status'], 'REMOVED')
        self.collect([])
        self.assertEqual(len(self.job()['lifecycle_history']), 2)
        self.assertEqual(self.store.count('job_postings'), 1)
        self.collect([RAW])
        self.assertEqual(self.job()['status'], 'ACTIVE')
        self.assertEqual(self.job()['description_revisions'], original)

    def test_partial_invalid_failed_and_truncated_inventory_do_not_remove(self):
        self.collect([RAW])
        self.collect([dict(RAW, id=99), dict(RAW, id=100, location=['invalid'])])
        self.assertEqual(self.job()['status'], 'ACTIVE')
        with self.assertRaises(ValueError):
            self.collect([], failed=True)
        self.assertEqual(self.job()['status'], 'ACTIVE')
        self.collect([], total=10)
        self.assertEqual(self.job()['status'], 'ACTIVE')

    def test_salary_only_changes_and_nonmaterial_metadata_refresh(self):
        self.collect([RAW])
        original = self.job()['description_revisions'][0]
        self.collect([dict(RAW, updated_at='not-a-date')])
        self.assertEqual(self.job()['description_revisions'], [original])
        self.collect([dict(RAW, pay_input_ranges=[dict(min_cents=9000000, max_cents=10000000, currency_type='USD')])])
        revisions = self.job()['description_revisions']
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]['description_hash'], revisions[1]['description_hash'])
        self.assertEqual(revisions[1]['salary']['min'], 90000)
        self.assertIsNone(self.job()['expires_at'])


from tests.integration import test_company_source_monitoring as monitoring_tests


class MonitoredLifecycleTests(unittest.TestCase):
    setUp = monitoring_tests.CompanySourceMonitoringTests.setUp
    tearDown = monitoring_tests.CompanySourceMonitoringTests.tearDown
    fetch = monitoring_tests.CompanySourceMonitoringTests.fetch
    configure = monitoring_tests.CompanySourceMonitoringTests.configure

    def test_worker_refresh_persists_explicit_closure_and_reopen_from_parsed_feed(self):
        from services.company_discovery.monitoring import queue_refresh
        from services.company_discovery.public_client import PublicCompanyClient
        from workers.tasks import execute_operation
        record_company_domain(self.store, self.company, 'employer.example',
                              'VERIFIED_EXISTING_DOMAIN', ['Independent employer record'])
        self.configure()
        for status in ('ACTIVE', 'CLOSED', 'ACTIVE'):
            payload = json.loads(self.pages['/jobs.json'])
            payload['items'][0]['status'] = status
            self.pages['/jobs.json'] = json.dumps(payload)
            with self.store.engine.begin() as c:
                c.execute(update(m.company_source_monitoring).values(last_attempt=None))
            queued = queue_refresh(self.store, self.source['id'], 'fixture')
            with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', self.fetch):
                result = execute_operation(queued['operation_id'])
            self.assertEqual(result['status'], 'COMPLETED', result)
            job = self.store.jobs()[0]
            self.assertEqual(job['status'], status)
        detail = self.store.job(job['id'])
        self.assertEqual(len(detail['lifecycle_history']), 3)
        self.assertEqual(len(detail['description_revisions']), 1)
        self.assertEqual(detail['lifecycle']['authority'], 'VERIFIED_EMPLOYER')
