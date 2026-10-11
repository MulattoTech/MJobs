"""Queued comparison survives HTTP/process lifetime without changing career facts."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_uploaded_resume_comparison as fixtures
from workers.operations import claim, recover_expired
from workers.tasks import execute_operation


class DurableUploadedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        url = getattr(self, 'database_url', f"sqlite:///{(self.root/'test.db').as_posix()}")
        self.store = Store(url)
        if hasattr(self, 'database_url'):
            # PostgreSQL wrapper owns an isolated schema and its migrations.
            pass
        else:
            from alembic import command
            from alembic.config import Config
            config = Config('alembic.ini')
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL': url}):
                command.upgrade(config, 'head')
        self.store.bind_workspace_mode('FIXTURE')
        from tests.integration.test_spine import PROFILE
        self.store.save_profile(PROFILE)

    tearDown = fixtures.UploadedResumeComparisonTests.tearDown
    target = fixtures.UploadedResumeComparisonTests.target
    upload = fixtures.UploadedResumeComparisonTests.upload

    def admission(self, client, app, target, upload, request_id='comparison-request-0001'):
        url = f'/profiles/fixture/applications/{app}/uploaded-resume-comparison/operations'
        response = client.post(url, params={
            'base_revision_id': upload['revision_id'], 'tailored_document_id': target['id'],
            'request_id': request_id,
        })
        self.assertEqual(response.status_code, 202, response.text)
        return url, response.json()

    def execute(self, operation_id):
        with patch.dict(os.environ, {
            'MJOBS_DATABASE_URL': self.store.engine.url.render_as_string(hide_password=False), 'MJOBS_OUTPUT_DIR': str(self.root),
            'MJOBS_OPERATION_MODE': 'FIXTURE',
        }):
            return execute_operation(operation_id)

    def test_admit_disconnect_execute_reopen_and_replay_preserves_result(self):
        app, target = self.target(); upload = self.upload()
        before = self.store.profile('fixture')
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            url, admitted = self.admission(client, app, target, upload)
            _, replay = self.admission(client, app, target, upload)
            self.assertEqual(replay['operation_id'], admitted['operation_id'])
            self.assertTrue(replay['replayed'])
        self.assertEqual(self.execute(admitted['operation_id'])['status'], 'COMPLETED')
        reopened = Store(self.store.engine.url.render_as_string(hide_password=False))
        try:
            with TestClient(create_app(reopened, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
                response = client.get(url, params={'base_revision_id': upload['revision_id'],
                    'tailored_document_id': target['id']})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result['state'], 'COMPLETED')
                self.assertEqual(result['result']['base']['sha256'], upload['sha256'])
                self.assertIn('Original summary', result['result']['base']['text'])
                _, replay = self.admission(client, app, target, upload)
                self.assertEqual(replay['state'], 'COMPLETED')
            self.assertEqual(reopened.profile('fixture'), before)
        finally:
            reopened.engine.dispose()

    def test_conflicting_intent_and_foreign_profile_are_rejected(self):
        app, target = self.target(); upload = self.upload(); other = self.upload('docx')
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            url, admitted = self.admission(client, app, target, upload)
            response = client.post(url, params={'base_revision_id': other['revision_id'],
                'tailored_document_id': target['id'], 'request_id': 'comparison-request-0001'})
            self.assertEqual(response.status_code, 409)
            from tests.integration.test_spine import PROFILE
            self.store.save_profile(dict(PROFILE, id='other'))
            foreign = url.replace('/fixture/', '/other/')
            self.assertEqual(client.get(foreign, params={'base_revision_id': upload['revision_id'],
                'tailored_document_id': target['id']}).status_code, 404)
            self.assertEqual(client.post(foreign+'/'+admitted['operation_id']+'/cancel').status_code, 404)

    def test_cancel_queued_survives_reopen_and_never_publishes_result(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            url, admitted = self.admission(client, app, target, upload)
            response = client.post(url+'/'+admitted['operation_id']+'/cancel')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['state'], 'CANCELLED')
        self.execute(admitted['operation_id'])
        with self.store.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(m.uploaded_comparison_results).where(
                m.uploaded_comparison_results.c.id == admitted['operation_id'])).first())

    def test_lease_crash_recovers_and_stale_attempt_cannot_publish(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            _, admitted = self.admission(client, app, target, upload)
        stale = claim(self.store, admitted['operation_id'])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == admitted['operation_id']).values(
                lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        self.assertEqual(self.execute(admitted['operation_id'])['status'], 'COMPLETED')
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.id == admitted['operation_id'])).mappings().one()
            self.assertEqual(row['attempt_count'], 2)
        from services.documents.durable_comparison import complete_result
        with self.store.engine.begin() as connection:
            self.assertFalse(complete_result(connection, stale, {'forged': True}))

    def test_changed_source_blocks_worker_and_hides_completed_result(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            url, admitted = self.admission(client, app, target, upload)
            self.assertEqual(self.execute(admitted['operation_id'])['status'], 'COMPLETED')
            (self.root/'uploads'/f"{upload['id']}.txt").write_bytes(b'Changed private bytes')
            response = client.get(url, params={'base_revision_id': upload['revision_id'], 'tailored_document_id': target['id']})
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('Changed private bytes', response.text)
        self.assertEqual(self.store.profile('fixture')['id'], 'fixture')

    def test_result_write_crash_rolls_back_completion_then_recovers_once(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            _, admitted = self.admission(client, app, target, upload)
        from services.documents.durable_comparison import complete_result
        def crash_after_insert(connection, operation, result):
            self.assertTrue(complete_result(connection, operation, result))
            raise RuntimeError('synthetic crash before transaction commit')
        with patch('workers.tasks.complete_result', side_effect=crash_after_insert):
            with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                self.execute(admitted['operation_id'])
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.operations.c.state).where(
                m.operations.c.id == admitted['operation_id'])).scalar_one(), 'RUNNING')
            self.assertIsNone(connection.execute(select(m.uploaded_comparison_results).where(
                m.uploaded_comparison_results.c.id == admitted['operation_id'])).first())
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == admitted['operation_id']).values(
                lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        self.assertEqual(self.execute(admitted['operation_id'])['status'], 'COMPLETED')
        self.assertEqual(self.execute(admitted['operation_id'])['status'], 'ALREADY_CLAIMED_OR_CAPACITY')

    def test_completion_cancel_race_discards_computed_result(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            _, admitted = self.admission(client, app, target, upload)
        from services.documents.durable_comparison import compare_for_operation
        from workers.operations import request_cancel
        def cancel_after_compute(store, output_dir, operation, check):
            result = compare_for_operation(store, output_dir, operation, check)
            request_cancel(self.store, operation['id'])
            return result
        with patch('workers.tasks.compare_for_operation', side_effect=cancel_after_compute):
            self.assertEqual(self.execute(admitted['operation_id'])['status'], 'CANCELLED')
        with self.store.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(m.uploaded_comparison_results).where(
                m.uploaded_comparison_results.c.id == admitted['operation_id'])).first())

    def test_expired_unreclaimed_lease_cannot_complete(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            _, admitted = self.admission(client, app, target, upload)
        from services.documents.durable_comparison import compare_for_operation
        def expire_after_compute(store, output_dir, operation, check):
            result = compare_for_operation(store, output_dir, operation, check)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == operation['id']).values(
                    lease_expires_at='2000-01-01T00:00:00+00:00'))
            return result
        with patch('workers.tasks.compare_for_operation', side_effect=expire_after_compute):
            self.assertEqual(self.execute(admitted['operation_id'])['status'], 'OWNERSHIP_EXPIRED')
        recover_expired(self.store)
        self.assertEqual(self.execute(admitted['operation_id'])['status'], 'COMPLETED')

    def test_three_expired_attempts_dead_letter_and_replay_does_not_reopen(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            _, admitted = self.admission(client, app, target, upload)
            for attempt in range(1, 4):
                self.assertEqual(claim(self.store, admitted['operation_id'])['attempt_count'], attempt)
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == admitted['operation_id']).values(
                        lease_expires_at='2000-01-01T00:00:00+00:00'))
                recover_expired(self.store)
            _, replay = self.admission(client, app, target, upload)
            self.assertEqual(replay['state'], 'DEAD_LETTERED')
            _, fresh = self.admission(client, app, target, upload, 'comparison-new-intent-0002')
            self.assertNotEqual(fresh['operation_id'], replay['operation_id'])
            self.assertEqual(fresh['state'], 'QUEUED')

    def test_latest_is_newest_intent_even_if_older_work_finishes_later(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            url, first = self.admission(client, app, target, upload)
            _, second = self.admission(client, app, target, upload, 'comparison-new-intent-0002')
            self.assertEqual(self.execute(first['operation_id'])['status'], 'COMPLETED')
            response = client.get(url, params={'base_revision_id': upload['revision_id'], 'tailored_document_id': target['id']})
            self.assertEqual(response.json()['operation_id'], second['operation_id'])

    def test_running_stage_and_progress_are_durable_and_fenced(self):
        app, target = self.target(); upload = self.upload()
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            _, admitted = self.admission(client, app, target, upload)
        from services.documents.uploaded_comparison import compare_uploaded_resume
        def inspect_running(*args):
            with self.store.engine.connect() as connection:
                current = connection.execute(select(m.operations).where(m.operations.c.id == admitted['operation_id'])).mappings().one()
                self.assertEqual(current['current_step'], 'EXTRACTING_AND_COMPARING')
                self.assertEqual(current['progress'], 20)
            return compare_uploaded_resume(*args)
        with patch('services.documents.durable_comparison.compare_uploaded_resume', side_effect=inspect_running):
            self.assertEqual(self.execute(admitted['operation_id'])['status'], 'COMPLETED')


class ComparisonMigrationTests(unittest.TestCase):
    def test_empty_downgrade_upgrade_and_populated_refusal(self):
        case = DurableUploadedComparisonTests()
        case.setUp()
        try:
            from alembic import command
            from alembic.config import Config
            config = Config('alembic.ini')
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL': str(case.store.engine.url)}):
                command.downgrade(config, '20261011rx01')
                command.upgrade(config, 'head')
                app, target = case.target(); upload = case.upload()
                with TestClient(create_app(case.store, 'http://127.0.0.1:1', case.root, operation_mode='FIXTURE')) as client:
                    _, admitted = case.admission(client, app, target, upload)
                self.assertEqual(case.execute(admitted['operation_id'])['status'], 'COMPLETED')
                with self.assertRaisesRegex(RuntimeError, 'Refusing to discard'):
                    command.downgrade(config, '20261011rx01')
        finally:
            case.tearDown()
