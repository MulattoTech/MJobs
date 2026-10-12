"""A reviewed aggregator employer can enroll without fabricated domain proof."""
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.requests import approve_discovery_request, list_discovery_requests
from services.discovery.scheduler import schedule_due_sources
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.greenhouse import Greenhouse
from services.discovery.sources.collect import configure_source, collect_source
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class ReviewedGreenhouseEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(getattr(self, 'database_url', f"sqlite:///{(self.root / 'review.db').as_posix()}"))
        if self.store.engine.dialect.name == 'sqlite':
            self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(PROFILE)
        self.source = configure_source(self.store, 'default', provider='remotive')
        self.env = {'MJOBS_DATABASE_URL': self.store.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
            'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(self.root / 'outputs')}
        save_search_profile(self.store, {'id': 'all-sources', 'profile_id': 'fixture',
            'name': 'All employers', 'enabled': True, 'source_ids': []})
        self.jobs = [{'id': 101, 'title': 'Python Engineer', 'content': '<p>Python remote role</p>',
            'location': {'name': 'Remote'}, 'absolute_url': 'https://job-boards.greenhouse.io/reviewed/jobs/101'}]
        self.fetches = []

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def collect(self):
        payload = dict(RAW_REMOTIVE_PAYLOAD, jobs=[RAW_REMOTIVE_PAYLOAD['jobs'][0]])
        collect_source(self.store, self.source, 'fixture',
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        request = list_discovery_requests(self.store)[0]
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one()
            self.company_id = row['company_id']
            upsert(connection, m.routes, {'id': 'reviewed-employer-original-route',
                'job_id': row['trigger_job_id'], 'company_id': self.company_id, 'provider': 'greenhouse',
                'tenant': 'reviewed', 'requisition': '101',
                'url': 'https://job-boards.greenhouse.io/reviewed/jobs/101'})
        return request

    def page(self, client, url, deadline):
        self.fetches.append(url)
        request = httpx.Request('GET', url)
        pages = {'/robots.txt': 'User-agent: *\nAllow: /',
            '/': '<a rel="careers" href="/careers">Careers</a>',
            '/careers': '<a href="https://job-boards.greenhouse.io/reviewed">Open jobs</a>'}
        return httpx.Response(200 if request.url.path in pages else 404,
            text=pages.get(request.url.path, ''), request=request)

    def board(self, adapter, suffix, params=None):
        self.fetches.append('board:' + adapter.tenant + suffix)
        return {'name': 'Different provider display name'} if not suffix else {'jobs': self.jobs}

    def enroll(self):
        request = self.collect()
        queued = approve_discovery_request(self.store, request['id'], 'https://employer.example/',
            request['revision'], confirm_company_identity=True)
        with patch.dict(os.environ, self.env), \
                patch.object(PublicCompanyClient, '_request_once', lambda client, url, deadline: self.page(client, url, deadline)), \
                patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None: self.board(adapter, suffix, params)):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'COMPLETED', result)
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.company_sources).where(
                m.company_sources.c.tenant == 'reviewed')).mappings().one())
        return request, source

    def test_reviewed_new_employer_enrolls_imports_and_reloads_recurring_binding(self):
        request, source = self.enroll()
        self.assertTrue(source['enabled'])
        self.assertEqual(source['company_id'], self.company_id)
        self.assertEqual(source['identity_authorization']['request_id'], request['id'])
        self.assertEqual(self.store.count('company_domains'), 0)
        reopened = Store(self.store.url)
        try:
            with reopened.engine.begin() as connection:
                connection.execute(update(m.job_sources).where(
                    m.job_sources.c.id == source['collector_source_id']).values(
                        last_attempt='2000-01-01T00:00:00+00:00'))
            scheduled = schedule_due_sources(reopened)
            operations = [row for row in scheduled['scheduled'] if row['source_id'] == source['collector_source_id']]
            self.assertEqual(len(operations), 1, scheduled)
            self.jobs.append(dict(self.jobs[0], id=102,
                absolute_url='https://job-boards.greenhouse.io/reviewed/jobs/102'))
            with patch.dict(os.environ, self.env), patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None: self.board(adapter, suffix, params)):
                result = execute_operation(operations[0]['operation_id'])
            self.assertEqual(result['status'], 'COMPLETED', result)
            with reopened.engine.connect() as connection:
                self.assertEqual(set(connection.execute(select(m.jobs.c.company_id)).scalars()), {self.company_id})
                self.assertEqual(len(connection.execute(select(m.jobs.c.id).where(
                    m.jobs.c.company_id == self.company_id)).all()), 2)
            self.assertEqual(reopened.count('company_domains'), 0)
        finally:
            reopened.engine.dispose()

    def test_revoked_review_blocks_scheduling_and_egress(self):
        from services.discovery.sources.collect import collect_source
        request, source = self.enroll()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id']).values(domain_evidence=[]))
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
        scheduled = schedule_due_sources(self.store)
        self.assertFalse(any(row['source_id'] == source['collector_source_id'] for row in scheduled['scheduled']))
        self.assertTrue(any(row.get('reason') == 'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED' for row in scheduled['skipped']))
        with patch.object(Greenhouse, '_get', side_effect=AssertionError('Revoked source must not fetch')):
            with self.assertRaisesRegex(Exception, 'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED'):
                collect_source(self.store, source['collector_source_id'], 'fixture')
        self.assertEqual(self.store.count('canonical_jobs'), 1)

    def test_review_revocation_during_fetch_prevents_import_without_provider_backoff(self):
        from services.discovery.sources.collect import collect_source
        request, source = self.enroll()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source['collector_source_id'])
                .values(last_attempt='2000-01-01T00:00:00+00:00'))
        self.jobs.append(dict(self.jobs[0], id=102, absolute_url='https://job-boards.greenhouse.io/reviewed/jobs/102'))
        def revoked(adapter, suffix, params=None):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_discovery_requests).where(
                    m.company_discovery_requests.c.id == request['id']).values(domain_evidence=[]))
            return self.board(adapter, suffix, params)
        with patch.object(Greenhouse, '_get', revoked):
            with self.assertRaisesRegex(Exception, 'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED'):
                collect_source(self.store, source['collector_source_id'], 'fixture')
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        with self.store.engine.connect() as connection:
            collector = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source['collector_source_id'])).mappings().one()
        self.assertEqual(collector['consecutive_failures'], 0)
        self.assertEqual(collector['health_status'], 'HEALTHY')

    def test_changed_authority_and_binding_fail_closed(self):
        from services.discovery.sources.collect import collect_source
        request, source = self.enroll()
        with self.store.engine.connect() as connection:
            original_request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one())
            original_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == original_request['operation_id'])).mappings().one())
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source['collector_source_id'])
                .values(last_attempt='2000-01-01T00:00:00+00:00'))
        changes = [
            (m.company_discovery_requests, request['id'], 'revision', original_request['revision'] + 1),
            (m.company_discovery_requests, request['id'], 'source_snapshot', {}),
            (m.company_discovery_requests, request['id'], 'selected_website', 'https://different.example/'),
            (m.company_discovery_requests, request['id'], 'operation_id', None),
            (m.company_sources, source['id'], 'career_url', 'https://different.example/careers'),
            (m.company_sources, source['id'], 'identity_authorization', {}),
            (m.company_sources, source['id'], 'endpoint', 'https://boards-api.greenhouse.io/v1/boards/foreign/jobs'),
            (m.company_discovery_requests, request['id'], 'domain_evidence', []),
            (m.operations, original_operation['id'], 'kind', 'DISCOVER'),
            (m.operations, original_operation['id'], 'entity_id', 'different-company'),
            (m.operations, original_operation['id'], 'params', dict(original_operation['params'], discovery_request_revision=0)),
            (m.operations, original_operation['id'], 'params', dict(original_operation['params'], candidate=None)),
            (m.operations, original_operation['id'], 'params', []),
        ]
        for table, row_id, field, value in changes:
            original = (original_request if table is m.company_discovery_requests else
                original_operation if table is m.operations else source)[field]
            with self.subTest(field=field):
                with self.store.engine.begin() as connection:
                    connection.execute(update(table).where(table.c.id == row_id).values({field: value}))
                with patch.object(Greenhouse, '_get', side_effect=AssertionError('Changed authority must not fetch')):
                    with self.assertRaisesRegex(Exception, 'DISCOVERY_.*CHANGED|COMPANY_DOMAIN_REVIEW_REQUIRED'):
                        collect_source(self.store, source['collector_source_id'], 'fixture')
                with self.store.engine.begin() as connection:
                    connection.execute(update(table).where(table.c.id == row_id).values({field: original}))
        self.assertEqual(self.store.count('canonical_jobs'), 1)

    def test_confirmation_does_not_override_paused_enrolled_source(self):
        from services.company_discovery.sources import set_company_source_enabled
        from services.discovery.sources.collect import collect_source
        _, source = self.enroll()
        set_company_source_enabled(self.store, source['id'], enabled=False)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
        scheduled = schedule_due_sources(self.store)
        self.assertFalse(any(row['source_id'] == source['collector_source_id'] for row in scheduled['scheduled']))
        with patch.object(Greenhouse, '_get', side_effect=AssertionError('Paused source must not fetch')):
            with self.assertRaisesRegex(Exception, 'COMPANY_SOURCE_DISABLED'):
                collect_source(self.store, source['collector_source_id'], 'fixture')
        set_company_source_enabled(self.store, source['id'], enabled=True)
        scheduled = schedule_due_sources(self.store)
        operations = [row for row in scheduled['scheduled'] if row['source_id'] == source['collector_source_id']]
        self.assertEqual(len(operations), 1, scheduled)
        with patch.dict(os.environ, self.env), patch.object(Greenhouse, '_get',
                lambda adapter, suffix, params=None: self.board(adapter, suffix, params)):
            self.assertEqual(execute_operation(operations[0]['operation_id'])['status'], 'COMPLETED')
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_unconfirmed_candidate_cannot_enroll_or_claim_original_employer(self):
        request = self.collect()
        queued = approve_discovery_request(self.store, request['id'], 'https://employer.example/',
            request['revision'], confirm_company_identity=False)
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once',
                lambda client, url, deadline: self.page(client, url, deadline)), \
                patch.object(Greenhouse, '_get', side_effect=AssertionError('Unconfirmed employer must not enumerate')):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['error_category'], 'COMPANY_DOMAIN_REVIEW_REQUIRED', result)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.company_sources.c.id).where(
                m.company_sources.c.tenant == 'reviewed')).all(), [])
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_conflicting_domain_owner_and_cancelled_review_block_collection(self):
        from services.company_discovery.domains import record_company_domain
        from services.discovery.sources.collect import collect_source
        request, source = self.enroll()
        with self.store.engine.connect() as connection:
            operation_id = connection.execute(select(m.company_discovery_requests.c.operation_id).where(
                m.company_discovery_requests.c.id == request['id'])).scalar_one()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(state='CANCELLED'))
        with patch.object(Greenhouse, '_get', side_effect=AssertionError('Cancelled authority must not fetch')):
            with self.assertRaisesRegex(Exception, 'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED'):
                collect_source(self.store, source['collector_source_id'], 'fixture')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(state='COMPLETED'))
            upsert(connection, m.companies, {'id': 'foreign-employer', 'name': 'Other Employer', 'domain': ''})
        record_company_domain(self.store, 'foreign-employer', 'employer.example',
            'VERIFIED_EXISTING_DOMAIN', ['Different employer independent evidence'])
        with patch.object(Greenhouse, '_get', side_effect=AssertionError('Conflicting owner must not fetch')):
            with self.assertRaisesRegex(Exception, 'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED'):
                collect_source(self.store, source['collector_source_id'], 'fixture')
        self.assertEqual(self.store.count('canonical_jobs'), 1)
