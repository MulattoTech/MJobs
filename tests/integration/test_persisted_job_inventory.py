"""Complete catalogue pages: real API, store, visibility and database reopen."""
import os
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url

from apps.api.main import create_app
from cli import compose_recovery as recovery
from packages.persistence import models as m
from packages.persistence.store import Store
from services.tracking.jobs import set_job_disposition
from tests.integration import test_recovery_inventory_completeness as legacy
from tests.integration.test_spine import PROFILE


class PersistedJobInventoryTests(unittest.TestCase):
    seed_jobs = legacy.PublicJobInventoryCompletenessTests.seed_jobs

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='mjobs-persisted-inventory-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.url = getattr(self, 'database_url', 'sqlite:///' + (self.root / 'test.db').as_posix())
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        if self.url.startswith('sqlite:'):
            self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(PROFILE)
        self.ids = self.seed_jobs(601)
        with self.store.engine.begin() as connection:
            copies = [dict(row) for row in connection.execute(select(m.postings)).mappings()]
            for row in copies:
                row.update(id='second-' + row['id'], source='second-controlled-source')
            connection.execute(m.postings.insert(), copies)
        self.client = self.client_for(self.store)

    def client_for(self, store):
        client = TestClient(create_app(store, 'http://127.0.0.1:1', self.root,
                                      'redis://127.0.0.1:1/0', operation_mode='FIXTURE'))
        self.addCleanup(client.close)
        return client

    def get_json(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def restart_storage(self):
        """SQLite closes/reopens; the optional PG subclass can restart its owned container."""

    def test_pages_include_over_500_hidden_and_blacklisted_jobs_and_every_copy_after_reopen(self):
        set_job_disposition(self.store, 'fixture', self.ids[0], 'HIDDEN', 'hide')
        set_job_disposition(self.store, 'fixture', self.ids[1], 'NOT_INTERESTED', 'dismiss')
        from services.matching.company_preferences import set_company_preference
        set_company_preference(self.store, 'fixture', 'inventory-company', 'BLACKLISTED', 'blacklist')
        self.assertEqual(self.client.get('/jobs?profile_id=fixture').json()['jobs'], [])
        first_page = self.get_json('/profiles/fixture/job-inventory?limit=200')
        self.assertEqual(first_page['total'], 601)
        self.assertEqual(len(first_page['jobs']), 200)
        before = recovery.collect_persisted_job_inventory(self.get_json, 'fixture')
        self.assertEqual(before['schema_version'], 4)
        self.assertEqual(before['job_count'], 601)
        self.assertEqual(before['source_copy_count'], 1202)
        self.assertEqual([row['job_id'] for row in before['jobs']], self.ids)
        for index, row in enumerate(before['jobs']):
            self.assertEqual([copy['posting_id'] for copy in row['source_copies']],
                [f'inventory-posting-{index:03d}', f'second-inventory-posting-{index:03d}'])
        self.store.engine.dispose()
        self.restart_storage()
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        self.client = self.client_for(reopened)
        after = recovery.collect_persisted_job_inventory(self.get_json, 'fixture')
        self.assertEqual(before, after)
        result = recovery.compare_public_job_inventory({'job_inventory': before}, before, after)
        self.assertEqual(result['result'], 'PASS')
        self.assertIn('ALL_PERSISTED_JOBS', result['scope'])
        for key in ('schema_version', 'profile_version', 'snapshot_sha256'):
            self.assertEqual(result[key], before[key])

    def test_changes_between_pages_refuse_insert_delete_and_same_id_content_changes(self):
        for change in ('insert', 'delete', 'canonical', 'copy', 'route'):
            with self.subTest(change=change):
                page = self.get_json('/profiles/fixture/job-inventory?limit=200')
                with self.store.engine.begin() as connection:
                    if change == 'insert':
                        row = dict(connection.execute(select(m.jobs).limit(1)).mappings().one())
                        row.update(id='new-persisted-job', requisition='new')
                        connection.execute(m.jobs.insert().values(**row))
                    elif change == 'delete':
                        connection.execute(m.jobs.delete().where(m.jobs.c.id == 'new-persisted-job'))
                    elif change == 'canonical':
                        connection.execute(m.jobs.update().where(m.jobs.c.id == self.ids[-1]).values(description='Changed'))
                    elif change == 'copy':
                        connection.execute(m.postings.update().where(m.postings.c.job_id == self.ids[-1]).values(raw={'changed': True}))
                    else:
                        connection.execute(m.routes.update().where(m.routes.c.job_id == self.ids[-1]).values(url='https://example.test/changed'))
                response = self.client.get('/profiles/fixture/job-inventory', params={'cursor': page['next_cursor']})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['detail'], 'JOB_INVENTORY_SNAPSHOT_CHANGED')

    def test_jobs_without_routes_or_copies_remain_in_complete_manifest(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.postings.delete().where(m.postings.c.job_id == self.ids[-1]))
            connection.execute(m.routes.delete().where(m.routes.c.job_id == self.ids[-1]))
        result = recovery.collect_persisted_job_inventory(self.get_json, 'fixture')
        self.assertEqual(result['job_count'], 601)
        self.assertEqual(result['jobs'][-1]['source_copy_count'], 0)

    def test_profile_cursor_binding_and_budgets_fail_closed(self):
        page = self.get_json('/profiles/fixture/job-inventory?limit=200')
        self.store.save_profile({**PROFILE, 'id': 'other'})
        self.assertEqual(self.client.get('/profiles/other/job-inventory', params={'cursor': page['next_cursor']}).status_code, 409)
        self.assertEqual(self.client.get('/profiles/missing/job-inventory').status_code, 404)
        self.assertEqual(self.client.get('/profiles/fixture/job-inventory?limit=201').status_code, 422)
        self.assertEqual(self.client.get('/profiles/fixture/job-inventory?cursor=invalid').status_code, 409)
        with patch('packages.persistence.job_inventory.MAX_JOBS', 600):
            response = self.client.get('/profiles/fixture/job-inventory')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], 'JOB_INVENTORY_BUDGET_EXCEEDED')

    def test_payload_budget_refusal_releases_read_snapshot(self):
        for setting in ('MAX_BYTES', 'MAX_ROW_BYTES'):
            with self.subTest(setting=setting):
                with patch('packages.persistence.job_inventory.' + setting, 64):
                    response = self.client.get('/profiles/fixture/job-inventory')
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['detail'], 'JOB_INVENTORY_BUDGET_EXCEEDED')
                # The failed scan must not strand a read transaction/cursor.
                page = self.get_json('/profiles/fixture/job-inventory')
                self.assertEqual(page['total'], 601)

    def test_immutable_snapshot_payload_change_is_detected(self):
        posting = 'inventory-posting-600'
        with self.store.engine.begin() as connection:
            connection.execute(m.posting_snapshots.insert().values(id='snapshot', posting_id=posting,
                raw={'content': 'first'}, sha256='a' * 64, retrieved_at='2026-01-01T00:00:00Z'))
        for changes in ({'raw': {'content': 'changed'}}, {'sha256': 'b' * 64}):
            with self.subTest(changes=changes):
                page = self.get_json('/profiles/fixture/job-inventory')
                with self.store.engine.begin() as connection:
                    connection.execute(m.posting_snapshots.update().where(m.posting_snapshots.c.id == 'snapshot').values(**changes))
                self.assertEqual(self.client.get('/profiles/fixture/job-inventory', params={'cursor': page['next_cursor']}).status_code, 409)

    def test_only_poll_metadata_is_ignored_and_malformed_attributes_are_controlled(self):
        def attributes(sequence, quality=90):
            return dict(first_seen_at='first', last_seen_at=str(sequence), last_collected_at=str(sequence),
                lifecycle=dict(status='ACTIVE', observation_id=str(sequence), observed_at=str(sequence)),
                source_resolution=dict(selected_posting_id='posting', selected_candidate_id=str(sequence),
                    evaluated_at=str(sequence), candidate_count=sequence, authority='VERIFIED_ATS', quality=quality,
                    candidates=[dict(id=str(sequence), observed_at=str(sequence), posting_id='posting', quality=quality)]))
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == self.ids[0]).values(attributes=attributes(1)))
        page = self.get_json('/profiles/fixture/job-inventory')
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == self.ids[0]).values(attributes=attributes(2)))
            connection.execute(m.postings.update().values(fetched_at='new-poll-time'))
        self.assertEqual(self.client.get('/profiles/fixture/job-inventory', params={'cursor': page['next_cursor']}).status_code, 200)
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == self.ids[0]).values(attributes=attributes(3, 99)))
        self.assertEqual(self.client.get('/profiles/fixture/job-inventory', params={'cursor': page['next_cursor']}).status_code, 409)
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == self.ids[0]).values(attributes={'source_resolution': {'candidates': [None]}}))
        response = self.client.get('/profiles/fixture/job-inventory')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], 'JOB_INVENTORY_INVALID_PERSISTED_DATA')


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'disposable PostgreSQL not configured')
class PersistedJobInventoryPostgresTests(PersistedJobInventoryTests):
    def test_manifest_uses_named_server_cursors_and_closes_them_on_budget_failure(self):
        cursors = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            if context.execution_options.get('yield_per'):
                cursors.append((cursor, getattr(cursor, 'name', None)))
        event.listen(self.store.engine, 'after_cursor_execute', observe)
        try:
            self.get_json('/profiles/fixture/job-inventory')
            with patch('packages.persistence.job_inventory.MAX_BYTES', 64):
                self.assertEqual(self.client.get('/profiles/fixture/job-inventory').status_code, 409)
        finally:
            event.remove(self.store.engine, 'after_cursor_execute', observe)
        self.assertTrue(cursors)
        self.assertTrue(all(name and cursor.closed for cursor, name in cursors),
                        'Every PostgreSQL scan must use and close an actual named server cursor')

    def restart_storage(self):
        """Opt-in actual restart, restricted to this run's disposable loopback PG."""
        container = os.environ.get('MJOBS_TEST_POSTGRES_RESTART_CONTAINER')
        if not container:
            return  # Still verifies a fresh Store/connection; not a server restart claim.
        owner = os.environ.get('MJOBS_TEST_POSTGRES_RESTART_OWNER', '')
        if not owner.startswith('han-inventory-pg-') or len(owner) != 29 or any(
                char not in '0123456789abcdef' for char in owner.removeprefix('han-inventory-pg-')):
            raise ValueError('Expected exact owned PostgreSQL restart context')
        def docker(*args):
            return subprocess.run(['docker', *args], check=True, capture_output=True, text=True, timeout=30).stdout
        info = json.loads(docker('inspect', container))[0]
        target = make_url(self.database_url)
        binding = info['NetworkSettings']['Ports']['5432/tcp']
        if (info['Id'] != container or info['Name'] != '/mjobs-test-' + owner
                or info['Config']['Labels'].get('mjobs.runtime.owner') != owner
                or len(binding) != 1 or binding[0]['HostIp'] != '127.0.0.1'
                or int(binding[0]['HostPort']) != target.port):
            raise ValueError('PostgreSQL restart target does not match this test database')
        before = info['State']['StartedAt']
        self.admin.dispose()
        docker('restart', '--time', '10', container)
        import time
        for _ in range(30):
            result = subprocess.run(['docker', 'exec', container, 'pg_isready', '-U', target.username,
                '-d', target.database], capture_output=True, timeout=5)
            if result.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise AssertionError('Owned PostgreSQL did not recover readiness')
        self.assertNotEqual(json.loads(docker('inspect', container))[0]['State']['StartedAt'], before)

    def setUp(self):
        url = make_url(os.environ['MJOBS_TEST_POSTGRES_URL'])
        if (url.get_backend_name() != 'postgresql' or url.host not in {'localhost', '127.0.0.1', '::1'}
                or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))
                or url.query):
            raise ValueError('An explicit loopback disposable mjobs_test database URL is required')
        self.schema = 'persisted_inventory_' + uuid4().hex
        self.admin = create_engine(url, connect_args={'connect_timeout': 5})
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.drop_schema)
        self.database_url = url.update_query_dict({'options': '-csearch_path=' + self.schema
            + ' -clock_timeout=5000 -cstatement_timeout=30000'}).render_as_string(hide_password=False)
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.database_url}):
            command.upgrade(Config('alembic.ini'), 'head')
        super().setUp()

    def drop_schema(self):
        suffix = self.schema.removeprefix('persisted_inventory_')
        if len(suffix) != 32 or any(char not in '0123456789abcdef' for char in suffix):
            raise ValueError('Unexpected disposable schema')
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
