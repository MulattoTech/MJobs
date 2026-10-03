import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from workers.dispatch import enqueue_operation


class TransactionalOutboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'test.db').as_posix()}")
        self.store.migrate()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_connection_rollback_does_not_leave_operation(self):
        with self.store.engine.connect() as connection:
            transaction = connection.begin()
            enqueue_operation(self.store, "DISCOVER", "rollback", {"n": 1}, connection=connection)
            transaction.rollback()
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_connection_commit_persists_for_new_store(self):
        with self.store.engine.connect() as connection:
            transaction = connection.begin()
            operation_id = enqueue_operation(self.store, "DISCOVER", "committed", {"n": 1}, connection=connection)
            transaction.commit()
        other = Store(self.store.url)
        try:
            with other.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one()
            self.assertEqual(row["state"], "QUEUED")
        finally:
            other.engine.dispose()

    def test_duplicate_does_not_reopen_completed_operation_or_replace_params(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "terminal", {"original": True})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                state="COMPLETED", params={"original": True}, error="kept"))
        self.assertEqual(operation_id, enqueue_operation(self.store, "DISCOVER", "terminal", {"original": True}))
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one()
        self.assertEqual(row["state"], "COMPLETED")
        self.assertEqual(row["params"], {"original": True})
        self.assertEqual(row["error"], "kept")

    def test_legacy_call_without_connection_still_commits(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "legacy", {})
        self.assertEqual(self.store.count("worker_operations"), 1)
        self.assertEqual(operation_id, enqueue_operation(self.store, "DISCOVER", "legacy", {}))


if __name__ == "__main__":
    unittest.main()
