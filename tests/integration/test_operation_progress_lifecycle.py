from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.domain.operation_types import REQUIRED_OPERATION_TYPES
from packages.persistence import models as m
from packages.persistence.store import Store
from packages.persistence.store import now
from services.operations.progress import get_operation_progress
from workers.dispatch import enqueue_operation
from workers.operations import admit_inline, claim, finish_inline


class OperationProgressLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mjobs-operation-lifecycle-')
        self.database = Path(self.directory.name) / 'operations.db'
        self.store = Store(f'sqlite:///{self.database.as_posix()}')
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_queued_worker_records_start_and_inline_completion_readback(self):
        queued = enqueue_operation(self.store, 'DISCOVER', 'profile-1', {'private':'never-return'})
        claimed = claim(self.store, queued)
        self.assertIsNotNone(claimed)
        running = get_operation_progress(self.store, queued)
        self.assertEqual(running['operation_type'], 'DISCOVER')
        self.assertEqual(running['state'], 'RUNNING')
        self.assertEqual(running['progress'], 1)
        self.assertEqual(running['current_step'], 'PREPARING')
        self.assertIsNotNone(running['started_at'])

        inline = admit_inline(self.store, 'AI_EVALUATE', 'profile-1')
        started = get_operation_progress(self.store, inline)
        self.assertEqual(started['operation_type'], 'AI_EVALUATE')
        self.assertEqual(started['progress'], 1)
        self.assertEqual(started['current_step'], 'PREPARING')
        self.assertIsNotNone(started['started_at'])
        self.assertTrue(finish_inline(self.store, inline, 'COMPLETED'))
        completed = get_operation_progress(self.store, inline)
        self.assertEqual(completed['state'], 'COMPLETED')
        self.assertEqual(completed['progress'], 100)
        self.assertEqual(completed['current_step'], 'COMPLETED')
        self.assertIsNotNone(completed['completed_at'])
        self.assertNotIn('private', completed)

    def test_invalid_inline_operation_types_are_redacted_and_never_persisted(self):
        for value in ('search_source', 'UNKNOWN_PRIVATE_SENTINEL', None, 7, True, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^OPERATION_TYPE_INVALID$') as raised:
                    admit_inline(self.store, value, 'profile-1')
                self.assertNotIn('UNKNOWN_PRIVATE_SENTINEL', repr(raised.exception))
                self.assertEqual(self.store.count('worker_operations'), 0)

    def test_canonical_types_without_inline_handlers_use_stable_unsupported_path(self):
        for operation_type in sorted(REQUIRED_OPERATION_TYPES):
            with self.subTest(operation_type=operation_type):
                with self.assertRaisesRegex(ValueError, '^Unsupported inline operation kind$'):
                    admit_inline(self.store, operation_type, 'profile-1')
                self.assertEqual(self.store.count('worker_operations'), 0)

    def test_claim_dead_letters_a_persisted_invalid_type_without_execution(self):
        operation_id = 'persisted-invalid-claim-type'
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id,
                kind='UNKNOWN_PRIVATE_SENTINEL',
                entity_id='profile-1',
                params={},
                state='QUEUED',
                error=None,
                updated_at=now(),
            ))

        self.assertIsNone(claim(self.store, operation_id))

        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one()
            action = connection.execute(select(m.audit.c.action).where(
                m.audit.c.entity_id == operation_id)).scalar_one()
        self.assertEqual(row['state'], 'DEAD_LETTERED')
        self.assertEqual(row['error'], 'OPERATION_TYPE_INVALID')
        self.assertEqual(row['current_step'], 'FINALIZING')
        self.assertIsNotNone(row['completed_at'])
        self.assertEqual(row['recovery_metadata'], {'action': 'MANUAL_REVIEW'})
        self.assertEqual(action, 'OPERATION_DEAD_LETTERED')

    def test_claim_dead_letters_a_canonical_type_without_a_runtime_handler(self):
        operation_id = 'persisted-unhandled-canonical-claim-type'
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id,
                kind='SEARCH_SOURCE',
                entity_id='profile-1',
                params={},
                state='QUEUED',
                error=None,
                updated_at=now(),
            ))

        self.assertIsNone(claim(self.store, operation_id))

        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one()
            action = connection.execute(select(m.audit.c.action).where(
                m.audit.c.entity_id == operation_id)).scalar_one()
        self.assertEqual(row['state'], 'DEAD_LETTERED')
        self.assertEqual(row['error'], 'OPERATION_TYPE_INVALID')
        self.assertEqual(row['current_step'], 'FINALIZING')
        self.assertIsNotNone(row['completed_at'])
        self.assertEqual(row['recovery_metadata'], {'action': 'MANUAL_REVIEW'})
        self.assertEqual(action, 'OPERATION_DEAD_LETTERED')


if __name__ == '__main__':
    unittest.main()
