"""T12 regressions for private certificate-store failures."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.browser_tree_ipc import BrowserTreeObserver
from workers.operations import admit_inline, OperationConcurrencyLimit


class BrowserCertificateDatabaseFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t12-certificate-")
        self.url = f"sqlite:///{(Path(self.directory.name) / 'fixture.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.token = "a" * 64
        self.operation = admit_inline(self.store, "MANUAL_AUTH", "synthetic-app",
            owner_token=self.token, manual_auth_identity={
                "profile_id": "synthetic-profile", "attempt_id": "synthetic-attempt"})
        self.binding = {
            "operation_id": self.operation, "application_id": "synthetic-app",
            "profile_id": "synthetic-profile", "attempt_id": "synthetic-attempt",
            "owner_digest": sha256(self.token.encode()).hexdigest(),
        }
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation)).mappings().one()
        owner = row["recovery_metadata"]["manual_auth_owner"]
        self.observer = object.__new__(BrowserTreeObserver)
        self.observer.identity = {
            "pid": 1234, "created": "1234", "host": owner["api_process"]["host"]}
        self.observer._binding = dict(self.binding)
        self.reply = {
            "binding": dict(self.binding), "status": "EXITED",
            "accounting": {"total": 3, "active": 0, "terminated": 0},
        }
        from services.applications import browser_tree_certificate as certificate
        self.certificate = certificate

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def row(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation)).mappings().one())

    def enroll(self):
        return self.certificate.enroll_observer(
            self.store, self.operation, self.observer, owner_token=self.token)

    def record(self):
        with patch.object(self.observer, "observe", return_value=deepcopy(self.reply)):
            return self.certificate.record_closure(
                self.store, self.operation, self.observer, owner_token=self.token)

    def assert_capacity_held(self):
        self.assertEqual(self.row()["state"], "RUNNING")
        with self.assertRaises(OperationConcurrencyLimit):
            admit_inline(self.store, "MANUAL_AUTH", "other-app")

    def test_connection_failure_is_boolean_and_healthy_retry_persists_readback(self):
        self.assertTrue(self.enroll())
        baseline = self.row()
        with patch.object(self.store.engine, "connect", side_effect=RuntimeError(
                "private synthetic database URL")):
            self.assertFalse(self.record())
        self.assertEqual(self.row(), baseline)
        self.assertNotIn("tree_closure", baseline["recovery_metadata"]["manual_auth_owner"])
        self.assert_capacity_held()

        self.assertTrue(self.record())
        persisted = self.row()["recovery_metadata"]["manual_auth_owner"]["tree_closure"]
        self.assertEqual(persisted["binding"], self.binding)
        self.assertEqual(persisted["observer"], self.observer.identity)
        self.assertEqual(persisted["accounting"], self.reply["accounting"])
        self.assertTrue(persisted["observed_at"])
        self.assert_capacity_held()

    def test_audit_failure_rolls_back_certificate_and_allows_healthy_retry(self):
        self.assertTrue(self.enroll())
        baseline = self.row()
        with patch.object(self.store, "_audit", side_effect=RuntimeError(
                "private synthetic audit failure")):
            self.assertFalse(self.record())
        failed = self.row()
        self.assertEqual(failed, baseline)
        self.assertNotIn("tree_closure", failed["recovery_metadata"]["manual_auth_owner"])
        self.assert_capacity_held()

        self.assertTrue(self.record())
        self.assertIn("tree_closure", self.row()["recovery_metadata"]["manual_auth_owner"])
        self.assert_capacity_held()

    def test_commit_failure_rolls_back_and_fresh_store_retry_reads_certificate(self):
        self.assertTrue(self.enroll())
        baseline = self.row()

        def fail_before_dbapi_commit(_connection):
            raise RuntimeError("private synthetic commit failure")

        event.listen(self.store.engine, "commit", fail_before_dbapi_commit, once=True)
        self.assertFalse(self.record())

        fresh = Store(self.url)
        try:
            with fresh.engine.connect() as connection:
                failed = dict(connection.execute(select(m.operations).where(
                    m.operations.c.id == self.operation)).mappings().one())
            self.assertEqual(failed, baseline)
            self.assertNotIn("tree_closure", failed["recovery_metadata"]["manual_auth_owner"])
            self.assertEqual(failed["state"], "RUNNING")
            with self.assertRaises(OperationConcurrencyLimit):
                admit_inline(fresh, "MANUAL_AUTH", "other-app")

            with patch.object(self.observer, "observe", return_value=deepcopy(self.reply)):
                self.assertTrue(self.certificate.record_closure(
                    fresh, self.operation, self.observer, owner_token=self.token))
            with fresh.engine.connect() as connection:
                persisted = connection.execute(select(m.operations).where(
                    m.operations.c.id == self.operation)).mappings().one()
            certificate = persisted["recovery_metadata"]["manual_auth_owner"]["tree_closure"]
            self.assertEqual(certificate["binding"], self.binding)
            self.assertEqual(certificate["observer"], self.observer.identity)
            self.assertEqual(certificate["accounting"], self.reply["accounting"])
            self.assertTrue(certificate["observed_at"])
            self.assertEqual(persisted["state"], "RUNNING")
            with self.assertRaises(OperationConcurrencyLimit):
                admit_inline(fresh, "MANUAL_AUTH", "another-app")
        finally:
            fresh.engine.dispose()

    def test_enrollment_connection_failure_returns_false_without_partial_owner_binding(self):
        baseline = self.row()
        with patch.object(self.store.engine, "begin", side_effect=RuntimeError(
                "private synthetic database URL")):
            self.assertFalse(self.enroll())
        failed = self.row()
        owner = failed["recovery_metadata"]["manual_auth_owner"]
        self.assertNotIn("tree_observer", owner)
        self.assertEqual(failed["state"], baseline["state"])
        self.assertTrue(self.enroll())
        self.assert_capacity_held()


if __name__ == "__main__":
    unittest.main()
