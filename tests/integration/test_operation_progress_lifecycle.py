from pathlib import Path
import tempfile
import unittest

from packages.persistence.store import Store
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
        self.assertEqual(running['state'], 'RUNNING')
        self.assertEqual(running['progress'], 1)
        self.assertEqual(running['current_step'], 'PREPARING')
        self.assertIsNotNone(running['started_at'])

        inline = admit_inline(self.store, 'AI_EVALUATE', 'profile-1')
        started = get_operation_progress(self.store, inline)
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


if __name__ == '__main__':
    unittest.main()
