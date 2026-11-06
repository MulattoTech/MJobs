"""Direct fictional employer monitoring; no provider network or Docker required."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import RedirectResponse
import httpx
from sqlalchemy import select, update

from apps.api.fixture_company_monitoring import create_fixture_company_monitoring_router
from cli.compose_recovery import collect_public_job_inventory
from fixtures.mock_server import fixture_server, create_mock_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery import fixture_monitoring as monitor
from services.company_discovery.monitoring import source_version
from services.discovery.pipeline import discover, MockClient
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.operations import claim


class FixtureCompanyMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store('sqlite:///' + (Path(self.directory.name) / 'fixture.db').as_posix())
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(PROFILE)
        save_search_profile(self.store, dict(id='reviewed', profile_id='fixture', name='Reviewed',
            enabled=True, source_ids=[], scheduler={'enabled': False, 'interval_seconds': 300}))
        self.requested_paths = []
        self.redirect_direct = False
        def observed_app(*args, **kwargs):
            app = create_mock_app(*args, **kwargs)
            @app.middleware('http')
            async def record_request(request, call_next):
                self.requested_paths.append(request.url.path)
                if self.redirect_direct and request.url.path == '/ats/example/jobs':
                    return RedirectResponse('/board/jobs', status_code=302)
                return await call_next(request)
            return app
        with patch('fixtures.mock_server.create_mock_app', side_effect=observed_app):
            self.server = fixture_server()
            self.origin = self.server.__enter__()
        discover(self.store, self.origin, 'fixture')
        with self.store.engine.connect() as connection:
            self.source = dict(connection.execute(select(m.company_sources)).mappings().one())

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.store.engine.dispose()
        self.directory.cleanup()

    def configure(self, **values):
        return monitor.configure_fixture_monitoring(self.store, self.origin, 'fixture', self.source['id'],
            source_version(self.source), 0, 300, **values)

    def queued(self):
        configured = self.configure()
        due = datetime.fromisoformat(configured['next_due'])
        row = monitor.schedule_fixture_sources(self.store, self.origin, at=due)['scheduled'][0]
        return claim(self.store, row['operation_id'])

    def test_review_natural_due_restart_and_duplicate_admission(self):
        configured = self.configure()
        due = datetime.fromisoformat(configured['next_due'])
        self.assertGreaterEqual(due, datetime.fromisoformat(self.source['last_success']) + timedelta(seconds=300))
        self.assertEqual(monitor.schedule_fixture_sources(self.store, self.origin, at=due-timedelta(seconds=1))['scheduled'], [])
        first = monitor.schedule_fixture_sources(self.store, self.origin, at=due)['scheduled']
        self.assertEqual(len(first), 1)
        reopened = Store(str(self.store.engine.url))
        try:
            self.assertEqual(monitor.schedule_fixture_sources(reopened, self.origin, at=due)['scheduled'], [])
        finally:
            reopened.engine.dispose()
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations)).mappings().one())
        self.assertEqual(operation['kind'], 'COMPANY_DISCOVER')
        self.assertEqual(operation['params']['profile_id'], 'fixture')
        self.assertEqual(operation['params']['fixture_source_id'], self.source['id'])
        self.assertEqual(operation['params']['scheduled_for'], configured['next_due'])

    def test_direct_refresh_preserves_jobs_and_does_not_fetch_board_or_rediscover(self):
        from apps.api.main import create_app
        operation = self.queued()
        before = sorted((j['id'], j['requisition'], j['posting_count']) for j in self.store.jobs('fixture'))
        with TestClient(create_app(self.store, self.origin, Path(self.directory.name), operation_mode='FIXTURE')) as api:
            inventory = collect_public_job_inventory(lambda path: api.get(path).json(), 'fixture')
        paths = []
        original = MockClient.get
        def observed(client, url):
            paths.append(url)
            return original(client, url)
        with patch.object(MockClient, 'get', observed):
            monitor.refresh_fixture_source(self.store, self.origin, operation)
            monitor.refresh_fixture_source(self.store, self.origin, operation)
        self.assertEqual(paths, [self.source['endpoint']])
        self.assertEqual(before, sorted((j['id'], j['requisition'], j['posting_count']) for j in self.store.jobs('fixture')))
        with TestClient(create_app(self.store, self.origin, Path(self.directory.name), operation_mode='FIXTURE')) as api:
            self.assertEqual(inventory, collect_public_job_inventory(lambda path: api.get(path).json(), 'fixture'))
        state = monitor.read_fixture_monitoring(self.store, self.origin, 'fixture', self.source['id'])
        self.assertEqual(state['status'], 'HEALTHY')
        self.assertEqual(state['completed_operation_id'], operation['id'])
        with self.store.engine.connect() as connection:
            self.assertTrue(monitor.fixture_refresh_saved(connection, operation))
            self.assertEqual(len(connection.execute(select(m.audit).where(m.audit.c.action == 'FIXTURE_COMPANY_SOURCE_REFRESHED')).all()), 1)

    def test_terminal_failure_gets_one_persisted_backoff_and_new_identity(self):
        operation = self.queued()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation['id']).values(state='FAILED'))
        stamp = datetime.now(timezone.utc)
        self.assertEqual(monitor.schedule_fixture_sources(self.store, self.origin, stamp)['scheduled'], [])
        backoff = monitor.read_fixture_monitoring(self.store, self.origin, 'fixture', self.source['id'])
        due = datetime.fromisoformat(backoff['next_due'])
        self.assertEqual(due, stamp + timedelta(seconds=600))
        self.assertEqual(monitor.schedule_fixture_sources(self.store, self.origin, stamp + timedelta(seconds=5))['scheduled'], [])
        self.assertEqual(monitor.read_fixture_monitoring(self.store, self.origin, 'fixture', self.source['id'])['next_due'], backoff['next_due'])
        retry = monitor.schedule_fixture_sources(self.store, self.origin, due)['scheduled'][0]
        self.assertNotEqual(retry['operation_id'], operation['id'])
        with self.store.engine.connect() as connection:
            retry_row = connection.execute(select(m.operations).where(m.operations.c.id == retry['operation_id'])).mappings().one()
        self.assertEqual(retry_row['params']['predecessor_operation_id'], operation['id'])

    def test_failed_network_attempt_records_attempt_without_success_receipt(self):
        operation = self.queued()
        with patch.object(MockClient, 'get', side_effect=httpx.ConnectError('synthetic outage')):
            with self.assertRaises(httpx.ConnectError):
                monitor.refresh_fixture_source(self.store, self.origin, operation)
        state = monitor.read_fixture_monitoring(self.store, self.origin, 'fixture', self.source['id'])
        self.assertIsNotNone(state['last_attempt'])
        self.assertIsNone(state['completed_operation_id'])
        with self.store.engine.connect() as connection:
            self.assertFalse(monitor.fixture_refresh_saved(connection, operation))

    def test_direct_endpoint_redirect_never_requests_aggregator(self):
        operation = self.queued()
        before = self.store.jobs('fixture')
        self.requested_paths.clear()
        self.redirect_direct = True
        with self.assertRaises(ValueError):
            monitor.refresh_fixture_source(self.store, self.origin, operation)
        self.assertEqual(self.requested_paths, ['/ats/example/jobs'])
        self.assertEqual(self.store.jobs('fixture'), before)
        with self.store.engine.connect() as connection:
            self.assertFalse(monitor.fixture_refresh_saved(connection, operation))

    def test_overflow_or_foreign_payload_commits_no_receipt(self):
        operation = self.queued()
        with self.store.engine.connect() as connection:
            before = len(connection.execute(select(m.postings)).all())
        original = MockClient.get
        for mutation in ('overflow', 'foreign', 'duplicate'):
            def altered(client, url):
                payload = original(client, url).json()
                if mutation == 'overflow':
                    payload['jobs'] = payload['jobs'] * 101
                elif mutation == 'foreign':
                    payload['jobs'][0]['company_website'] = self.origin + '/other-company'
                else:
                    payload['jobs'].append(payload['jobs'][0])
                return httpx.Response(200, json=payload, request=httpx.Request('GET', url))
            with self.subTest(mutation=mutation), patch.object(MockClient, 'get', altered):
                with self.assertRaises(ValueError):
                    monitor.refresh_fixture_source(self.store, self.origin, operation)
        with self.store.engine.connect() as connection:
            self.assertFalse(monitor.fixture_refresh_saved(connection, operation))
            self.assertEqual(before, len(connection.execute(select(m.postings)).all()))

    def test_settings_pause_cancellation_and_expired_lease_block_collection(self):
        operation = self.queued()
        for values in ({'cancel_requested': True}, {'lease_expires_at': '2000-01-01T00:00:00+00:00'}):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == operation['id']).values(**values))
            with patch.object(MockClient, 'get', side_effect=AssertionError('unexpected network')):
                with self.assertRaises(ValueError):
                    monitor.refresh_fixture_source(self.store, self.origin, operation)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == operation['id']).values(
                    cancel_requested=False, lease_expires_at=operation['lease_expires_at']))
        save_search_profile(self.store, dict(id='reviewed', profile_id='fixture', name='Reviewed', enabled=False))
        with patch.object(MockClient, 'get', side_effect=AssertionError('unexpected network')):
            with self.assertRaisesRegex(ValueError, 'SEARCH_PAUSED'):
                monitor.refresh_fixture_source(self.store, self.origin, operation)

    def test_changes_after_queue_block_before_network(self):
        for field, changed in [('endpoint', self.origin + '/wrong'), ('enabled', False)]:
            with self.subTest(field=field):
                # Restore source between cases; reuse one scheduled operation.
                if field == 'endpoint':
                    operation = self.queued()
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.source['id']).values(**{field: changed}))
                with patch.object(MockClient, 'get', side_effect=AssertionError('unexpected network')):
                    with self.assertRaises(ValueError):
                        monitor.refresh_fixture_source(self.store, self.origin, operation)
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.source['id']).values(**{field: self.source[field]}))

    def test_profile_edit_invalidates_review(self):
        operation = self.queued()
        self.store.save_profile(dict(PROFILE, name='Changed profile'))
        with patch.object(MockClient, 'get', side_effect=AssertionError('unexpected network')):
            with self.assertRaisesRegex(ValueError, 'PROFILE_CHANGED'):
                monitor.refresh_fixture_source(self.store, self.origin, operation)

    def test_import_fence_rolls_back_when_source_changes_during_fetch(self):
        operation = self.queued()
        original = MockClient.get
        def changed(client, url):
            result = original(client, url)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.source['id']).values(enabled=False))
            return result
        with patch.object(MockClient, 'get', changed):
            with self.assertRaises(ValueError):
                monitor.refresh_fixture_source(self.store, self.origin, operation)
        with self.store.engine.connect() as connection:
            self.assertFalse(monitor.fixture_refresh_saved(connection, operation))

    def test_wrong_origin_foreign_profile_and_stale_review_rejected(self):
        for origin in ['https://example.com', self.origin + '/extra', self.origin + '?x=1', 'http://127.0.0.1:1']:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                monitor.configure_fixture_monitoring(self.store, origin, 'fixture', self.source['id'], source_version(self.source), 0, 300)
        self.store.save_profile(dict(PROFILE, id='foreign'))
        with self.assertRaises(ValueError):
            monitor.configure_fixture_monitoring(self.store, self.origin, 'foreign', self.source['id'], source_version(self.source), 0, 300)
        self.configure()
        with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT'):
            self.configure()

    def test_api_strictness_and_personal_guard(self):
        def client(mode):
            app = FastAPI()
            app.include_router(create_fixture_company_monitoring_router(self.store, self.origin, mode))
            return TestClient(app)
        path = '/profiles/fixture/company-sources/' + self.source['id'] + '/fixture-monitoring'
        body = dict(source_version=source_version(self.source), revision=0, poll_interval=300)
        with client('FIXTURE') as api:
            self.assertEqual(api.put(path, json={**body, 'poll_interval': 1}).status_code, 422)
            self.assertEqual(api.put(path, json={**body, 'unexpected': True}).status_code, 422)
            self.assertEqual(api.put(path, json=body).status_code, 200)
            self.assertEqual(api.get(path).json()['profile_id'], 'fixture')
        with client('PERSONAL') as api:
            self.assertEqual(api.put(path, json=body).status_code, 409)


if __name__ == '__main__':
    unittest.main()
