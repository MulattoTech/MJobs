"""Real intake/rescoring creates durable company review, never guessed-site egress."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.matching import recalculate
from services.discovery.company_feed import import_company_feed_jobs
from services.company_discovery.domains import record_company_domain
from services.company_discovery.requests import list_discovery_requests, approve_discovery_request, dismiss_discovery_request
from services.company_discovery.public_client import PublicCompanyClient
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class DiscoveryRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'requests.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(copy.deepcopy(PROFILE))
        self.source = configure_source(self.store, 'example')
        self.env = {'MJOBS_DATABASE_URL': self.store.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
                    'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(self.root / 'outputs')}

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def collect(self, jobs=None):
        def response(request):
            return httpx.Response(200, json={'jobs': jobs or [RAW]} if request.url.path.endswith('/jobs') else {'name': 'Example Public Employer'})
        return collect_source(self.store, self.source, 'fixture', transport=httpx.MockTransport(response))

    def request(self):
        rows = list_discovery_requests(self.store)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def verify_domain(self):
        company = identity('greenhouse', 'example')
        with self.store.engine.begin() as c:
            upsert(c, m.companies, {'id': company, 'name': 'Example', 'domain': ''})
        return record_company_domain(self.store, company, 'employer.example', 'VERIFIED_EXISTING_DOMAIN', ['Independent company record'])

    def test_collection_and_rescore_coalesce_review_without_network_or_name_binding(self):
        with patch.object(PublicCompanyClient, 'get', side_effect=AssertionError('No employer egress')):
            self.collect([RAW, dict(RAW, id=1235)])
            first = self.request()
            recalculate(self.store, 'fixture')
        current = self.request()
        self.assertEqual(current['id'], first['id'])
        self.assertEqual(current['status'], 'NEEDS_ORIGIN_REVIEW')
        self.assertEqual(current['highest_match_score'], 100)
        self.assertIn('HIGH_MATCH_JOB', current['reasons'])
        self.assertIsNone(current['selected_website'])
        self.assertEqual(self.store.count('worker_operations'), 0)

    def test_verified_origin_queues_atomically_and_revocation_blocks_before_get(self):
        domain = self.verify_domain()
        self.collect()
        row = self.request()
        self.assertEqual(row['status'], 'QUEUED')
        self.assertEqual(row['authorization'], 'VERIFIED_DOMAIN')
        with self.store.engine.begin() as c:
            c.execute(update(m.company_domains).where(m.company_domains.c.id == domain['id']).values(verified=False))
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, 'get', side_effect=AssertionError('Revoked authorization')):
            result = execute_operation(row['operation_id'])
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(self.request()['status'], 'BLOCKED')

    def test_user_review_is_explicit_versioned_and_cannot_merge_by_name(self):
        self.collect()
        row = self.request()
        result = approve_discovery_request(self.store, row['id'], 'https://employer.example/', row['revision'])
        with self.assertRaisesRegex(ValueError, 'REVISION'):
            approve_discovery_request(self.store, row['id'], 'https://other.example/', row['revision'])
        current = self.request()
        self.assertEqual(current['authorization'], 'USER_CANDIDATE')
        with self.store.engine.connect() as c:
            operation = c.execute(select(m.operations).where(m.operations.c.id == result['operation_id'])).mappings().one()
        self.assertNotEqual(operation['entity_id'], identity('greenhouse', 'example'))
        self.assertEqual(self.store.count('company_domains'), 0)
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_user_cannot_assign_another_verified_employer_to_request(self):
        self.collect()
        with self.store.engine.begin() as c:
            upsert(c, m.companies, {'id': 'other-company', 'name': 'Other Company', 'domain': ''})
        record_company_domain(self.store, 'other-company', 'other.example', 'VERIFIED_EXISTING_DOMAIN', ['Other employer evidence'])
        row = self.request()
        with self.assertRaisesRegex(ValueError, 'COMPANY_IDENTITY'):
            approve_discovery_request(self.store, row['id'], 'https://other.example/', row['revision'])
        self.assertEqual(self.store.count('worker_operations'), 0)

    def test_disabled_source_blocks_review_and_dismissal_survives_rescore(self):
        self.collect()
        row = self.request()
        with self.store.engine.begin() as c:
            c.execute(update(m.job_sources).where(m.job_sources.c.id == self.source).values(enabled=False))
        with self.assertRaisesRegex(ValueError, 'SOURCE_DISABLED'):
            approve_discovery_request(self.store, row['id'], 'https://employer.example/', row['revision'])
        dismiss_discovery_request(self.store, row['id'], row['revision'])
        recalculate(self.store, 'fixture')
        self.assertEqual(self.request()['status'], 'DISMISSED')
        self.assertEqual(self.store.count('worker_operations'), 0)

    def test_request_and_operation_rollback_with_collection_transaction(self):
        self.verify_domain()
        from services.company_discovery.requests import record_discovery_request
        def fail_after(*args, **kwargs):
            record_discovery_request(*args, **kwargs)
            raise RuntimeError('synthetic rollback after queue')
        with patch('services.company_discovery.requests.record_discovery_request', side_effect=fail_after):
            with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
                self.collect()
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        self.assertEqual(self.store.count('company_discovery_requests'), 0)
        self.assertEqual(self.store.count('worker_operations'), 0)

    def test_fresh_source_intake_renews_review_but_preserves_terminal_operation(self):
        self.collect()
        original = self.request()
        queued = approve_discovery_request(self.store, original['id'], 'https://employer.example/', original['revision'])
        with self.store.engine.begin() as c:
            c.execute(update(m.operations).where(m.operations.c.id == queued['operation_id']).values(state='BLOCKED', error='controlled old failure'))
        recalculate(self.store, 'fixture')
        self.assertEqual(self.request()['operation_id'], queued['operation_id'])
        endpoint = 'https://new.example/jobs.json'
        with self.store.engine.begin() as c:
            source = dict(c.execute(select(m.company_sources)).mappings().one())
            c.execute(update(m.company_sources).where(m.company_sources.c.id == source['id']).values(endpoint=endpoint))
        # A changed source alone cannot authorize historical posting evidence.
        recalculate(self.store, 'fixture')
        self.assertEqual(self.request()['operation_id'], queued['operation_id'])
        import_company_feed_jobs(self.store, company_id=source['company_id'], source_id=source['id'],
            provider=source['provider'], tenant=source['tenant'], endpoint=endpoint, strategy='PUBLIC_JSON',
            items=[{'id': 'fresh-role', 'title': 'Python Engineer', 'content_text': 'Python SQL',
                    'url': 'https://new.example/roles/fresh-role'}], profile_id='fixture')
        recalculate(self.store, 'fixture')
        renewed = self.request()
        self.assertEqual(renewed['status'], 'NEEDS_ORIGIN_REVIEW')
        self.assertIsNone(renewed['operation_id'])
        self.assertGreater(renewed['revision'], queued['revision'])
        with self.store.engine.connect() as c:
            old = c.execute(select(m.operations).where(m.operations.c.id == queued['operation_id'])).mappings().one()
            audit = c.execute(select(m.audit.c.payload).where(m.audit.c.action == 'COMPANY_DISCOVERY_REQUEST_RENEWED')).scalar_one()
        self.assertEqual(old['state'], 'BLOCKED')
        self.assertEqual(audit['previous_operation_id'], old['id'])
        approved = approve_discovery_request(self.store, renewed['id'], 'https://new.example/', renewed['revision'])
        self.assertNotEqual(approved['operation_id'], old['id'])

    def test_disabled_source_after_queue_blocks_before_egress_and_restart_retains_request(self):
        self.collect()
        row = self.request()
        queued = approve_discovery_request(self.store, row['id'], 'https://employer.example/', row['revision'])
        reopened = Store(self.store.url)
        try:
            self.assertEqual(list_discovery_requests(reopened)[0]['operation_id'], queued['operation_id'])
        finally:
            reopened.engine.dispose()
        with self.store.engine.begin() as c:
            c.execute(update(m.job_sources).where(m.job_sources.c.id == self.source).values(enabled=False))
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, 'get', side_effect=AssertionError('Disabled source egress')):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'BLOCKED')
        recalculate(self.store, 'fixture')
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_verified_origin_automatic_worker_imports_same_company(self):
        self.verify_domain()
        self.collect()
        queued = self.request()
        pages = {
            '/robots.txt': 'User-agent: *\nAllow: /',
            '/': '<a rel="careers" href="/careers">Careers</a>',
            '/careers': '<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            '/jobs.json': '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"extra-role","title":"Python Engineer","content_text":"Python SQL","url":"https://employer.example/jobs/extra"}]}',
            '/jobs/extra': '<html>No structured evidence</html>',
        }
        def response(client, url, deadline):
            request = httpx.Request('GET', url)
            return httpx.Response(200, text=pages[request.url.path], request=request)
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', response):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'COMPLETED', result)
        with self.store.engine.connect() as c:
            company_ids = set(c.execute(select(m.jobs.c.company_id)).scalars())
        self.assertEqual(company_ids, {identity('greenhouse', 'example')})
        self.assertEqual(self.store.count('canonical_jobs'), 2)
        recalculate(self.store, 'fixture')
        # One request per source, and no repeated operation for the original source.
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_stale_bulk_candidate_cannot_change_request_lock_target(self):
        self.collect()
        row = self.request()
        from services.company_discovery.requests import record_discovery_request
        with self.store.engine.begin() as c:
            job_id = c.execute(select(m.jobs.c.id)).scalar_one()
            result = record_discovery_request(c, job_id, self.store.profile('fixture'),
                is_new_company=True, expected_candidate_id='superseded-candidate')
        self.assertIsNone(result)
        self.assertEqual(self.request(), row)
