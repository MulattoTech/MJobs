"""Durable observer enrollment and closure fencing; capacity remains held."""
from copy import deepcopy
from hashlib import sha256
import json
import os
import subprocess
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import select, update
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.browser_tree_ipc import BrowserTreeObserver
from services.operations.progress import operation_public_record
from workers.operations import admit_inline, OperationConcurrencyLimit


class BrowserTreeCertificateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-tree-certificate-")
        self.url = f"sqlite:///{(Path(self.directory.name) / 'fixture.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.token = "a" * 64
        self.operation = admit_inline(self.store, "MANUAL_AUTH", "synthetic-app",
            owner_token=self.token, manual_auth_identity={"profile_id": "synthetic-profile", "attempt_id": "synthetic-attempt"})
        self.binding = {"operation_id": self.operation, "application_id": "synthetic-app",
            "profile_id": "synthetic-profile", "attempt_id": "synthetic-attempt",
            "owner_digest": sha256(self.token.encode()).hexdigest()}
        self.observer = object.__new__(BrowserTreeObserver)
        self.observer.identity = {"pid": 1234, "created": "1234", "host": self.row()["recovery_metadata"]["manual_auth_owner"]["api_process"]["host"]}
        self.observer._binding = dict(self.binding)
        self.reply = {"binding": dict(self.binding), "status": "EXITED",
                      "accounting": {"total": 3, "active": 0, "terminated": 0}}

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def module(self):
        import importlib
        try:
            return importlib.import_module("services.applications.browser_tree_certificate")
        except ModuleNotFoundError:
            self.fail("Durable bound observer evidence is missing")

    def row(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == self.operation)).mappings().one())

    def persist(self):
        with patch.object(self.observer, "observe", return_value=deepcopy(self.reply)):
            return self.module().record_closure(self.store, self.operation, self.observer, owner_token=self.token)

    def enroll(self):
        return self.module().enroll_observer(self.store, self.operation, self.observer, owner_token=self.token)

    def test_durable_private_certificate_reload_idempotency_and_held_capacity(self):
        module = self.module()
        self.assertTrue(self.enroll())
        self.assertTrue(self.persist())
        first = self.row()
        certificate = first["recovery_metadata"]["manual_auth_owner"]["tree_closure"]
        self.assertEqual(certificate["binding"], self.binding)
        self.assertEqual(certificate["observer"], self.observer.identity)
        self.assertEqual(certificate["accounting"], self.reply["accounting"])
        self.assertTrue(certificate["observed_at"])
        self.assertEqual(first["state"], "RUNNING")
        self.assertTrue(self.persist())
        self.assertEqual(self.row(), first)
        peer = Store(self.url)
        try:
            with patch.object(self.observer, "observe", return_value=deepcopy(self.reply)):
                self.assertTrue(module.record_closure(peer, self.operation, self.observer, owner_token=self.token))
            self.assertNotIn("tree_closure", str(operation_public_record(self.row())))
            with self.assertRaises(OperationConcurrencyLimit):
                admit_inline(peer, "MANUAL_AUTH", "another-app")
        finally:
            peer.engine.dispose()

    def test_unenrolled_wrong_token_or_replaced_observer_cannot_certify(self):
        module = self.module()
        self.assertFalse(self.persist())
        self.assertFalse(module.enroll_observer(self.store, self.operation, self.observer, owner_token="c" * 64))
        self.assertTrue(self.enroll())
        self.observer.identity = {**self.observer.identity, "created": "5678"}
        self.assertFalse(self.enroll())
        self.assertFalse(self.persist())
        self.assertNotIn("tree_closure", str(self.row()))

    def test_missing_live_terminated_malformed_and_wrong_scope_fail_closed(self):
        self.assertTrue(self.enroll())
        baseline = self.row()
        bad = [None, {}, {**self.reply, "status": "UNKNOWN"},
               {**self.reply, "status": "LIVE", "accounting": {"total": 3, "active": 1, "terminated": 0}},
               {**self.reply, "accounting": {"total": 3, "active": 0, "terminated": 1}},
               {**self.reply, "accounting": {"total": 0, "active": 0, "terminated": 0}},
               {**self.reply, "accounting": {"total": 3, "active": False, "terminated": 0}}]
        bad += [{**self.reply, "binding": {**self.binding, key: "wrong"}} for key in self.binding]
        for reply in bad:
            with self.subTest(reply=reply):
                self.reply = reply
                self.assertFalse(self.persist())
                self.assertEqual(self.row(), baseline)

    def test_owner_changes_during_observation_are_not_overwritten(self):
        module = self.module()
        self.assertTrue(self.enroll())
        def observe(scope):
            row = self.row()
            metadata = row["recovery_metadata"]
            metadata["manual_auth_owner"]["attempt_id"] = "successor-attempt"
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == self.operation).values(recovery_metadata=metadata))
            return self.reply
        with patch.object(self.observer, "observe", side_effect=observe):
            self.assertFalse(module.record_closure(self.store, self.operation, self.observer, owner_token=self.token))
        self.assertNotIn("tree_closure", str(self.row()))
        self.assertIn("successor-attempt", str(self.row()))

    def test_expired_blocked_owner_can_record_but_terminal_owner_cannot(self):
        self.assertTrue(self.enroll())
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation).values(
                state="BLOCKED", error="BROWSER_RELEASE_UNCONFIRMED"))
        self.assertTrue(self.persist())
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation).values(state="COMPLETED"))
        self.assertFalse(self.persist())

    @unittest.skipUnless(sys.platform == "win32", "Native Windows private observer")
    def test_actual_chromium_closure_survives_query_close_and_database_reload(self):
        from services.applications.browser_tree import BrowserTreeJob
        from tests.unit.test_browser_tree_containment import eventually

        module = self.module()
        job = BrowserTreeJob()
        query = job.query_handle()
        observer = None
        location = Path(self.directory.name)
        stop = location / "release"
        try:
            observer = BrowserTreeObserver(query, self.binding)
            self.assertTrue(module.enroll_observer(self.store, self.operation, observer, owner_token=self.token))
            self.assertFalse(module.record_closure(self.store, self.operation, observer, owner_token=self.token))
            job.launch([sys.executable,
                str(Path(__file__).resolve().parents[1] / "fixtures" / "browser_tree_child.py"),
                "browser", str(location)], cwd=Path.cwd())
            self.assertTrue(eventually(lambda: (location / "ready.json").exists()))
            ready = json.loads((location / "ready.json").read_text())
            self.assertGreaterEqual(len(ready["members"]), 2)
            for pid in ready["members"]:
                self.assertTrue(query.contains(pid))
            self.assertTrue(eventually(lambda: observer.observe()["status"] == "LIVE"))
            self.assertFalse(module.record_closure(self.store, self.operation, observer, owner_token=self.token))
            job.close()
            query.close()
            stop.touch()
            self.assertTrue(eventually(lambda: observer.observe()["status"] == "EXITED"))
            peer = Store(self.url)
            try:
                self.assertTrue(module.record_closure(peer, self.operation, observer, owner_token=self.token))
                self.assertEqual(self.row()["recovery_metadata"]["manual_auth_owner"]["tree_closure"]["accounting"]["active"], 0)
                observer.close()
                self.assertFalse(module.record_closure(peer, self.operation, observer, owner_token=self.token))
            finally:
                peer.engine.dispose()
        finally:
            stop.touch()
            if observer is not None:
                observer.close()
            query.close()
            job.close()

    def test_foreign_host_observer_cannot_enroll_or_record(self):
        self.observer.identity["host"] = "f" * 64
        self.assertFalse(self.enroll())
        self.assertFalse(self.persist())
        self.assertNotIn("tree_observer", str(self.row()))

    @unittest.skipUnless(sys.platform == "win32", "Native retained query writer")
    def test_retained_native_writer_requires_self_enrollment_and_closed_tree(self):
        from services.applications.browser_process import capture_owner_identity
        from services.applications.browser_tree import BrowserTreeJob
        from tests.unit.test_browser_tree_containment import eventually
        module = self.module()
        self.observer.identity = capture_owner_identity()
        job = BrowserTreeJob()
        query = job.query_handle()
        location = Path(self.directory.name)
        try:
            self.assertTrue(module.enroll_retained_observer(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            job.launch([sys.executable, str(Path("tests/fixtures/browser_tree_child.py").resolve()),
                        "root", str(location)], cwd=Path.cwd())
            eventually(lambda: (location / "ready.json").exists())
            self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            (location / "release").touch()
            eventually(lambda: query.status() == "EXITED")
            self.assertTrue(module.record_retained_closure(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            self.assertEqual(self.row()["state"], "RUNNING")
            query.close()
            self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                self.binding, owner_token=self.token))
        finally:
            (location / "release").touch()
            query.close()
            job.close()

    def test_retained_writer_rejects_wire_proof_without_native_authority(self):
        module = self.module()
        self.assertTrue(self.enroll())
        baseline = self.row()
        for query in (None, self.reply, self.observer):
            with self.subTest(query=type(query).__name__):
                self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                    self.binding, owner_token=self.token))
                self.assertEqual(self.row(), baseline)

    def test_retained_writer_fences_identity_scope_native_failure_and_changed_owner(self):
        from services.applications.browser_process import capture_owner_identity
        from services.applications.browser_tree import BrowserTreeQuery
        module = self.module()
        self.observer.identity = capture_owner_identity()
        query = object.__new__(BrowserTreeQuery)
        query._retention_id = "d" * 64
        with patch.object(query, "contains", return_value=False), \
                patch.object(query, "limit_flags", return_value=0), \
                patch.object(query, "accounting", return_value=self.reply["accounting"]):
            self.assertTrue(module.enroll_retained_observer(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            baseline = self.row()
            for scope in ({}, {**self.binding, "attempt_id": "wrong"}):
                with self.subTest(scope=scope):
                    self.assertFalse(module.record_retained_closure(self.store, self.operation,
                        query, scope, owner_token=self.token))
            self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                self.binding, owner_token="c" * 64))
            for membership in (None, True):
                with self.subTest(membership=membership), patch.object(query, "contains", return_value=membership):
                    self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                        self.binding, owner_token=self.token))
            with patch.object(query, "limit_flags", return_value=0x2000):
                self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                    self.binding, owner_token=self.token))
            for accounting in (None, {"total": 3, "active": 0, "terminated": 1}):
                with self.subTest(accounting=accounting), patch.object(query, "accounting", return_value=accounting):
                    self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                        self.binding, owner_token=self.token))
            with patch.object(query, "accounting", side_effect=OSError("synthetic-private")):
                self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                    self.binding, owner_token=self.token))
            from services.applications import browser_process
            with patch.object(browser_process, "capture_owner_identity", return_value={
                    **self.observer.identity, "created": "different"}):
                self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                    self.binding, owner_token=self.token))
            self.assertEqual(self.row(), baseline)

            def changed_owner():
                metadata = self.row()["recovery_metadata"]
                metadata["manual_auth_owner"]["attempt_id"] = "successor-attempt"
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == self.operation)
                        .values(recovery_metadata=metadata))
                return self.reply["accounting"]
            with patch.object(query, "accounting", side_effect=changed_owner):
                self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                    self.binding, owner_token=self.token))
            self.assertNotIn("tree_closure", str(self.row()))
            self.assertIn("successor-attempt", str(self.row()))

    @unittest.skipUnless(sys.platform == "win32", "Native creator death and Chromium")
    def test_retained_writer_persists_after_actual_creator_death_and_peer_reload(self):
        from services.applications.browser_process import capture_owner_identity, owner_process_status
        from services.applications.browser_tree import BrowserTreeQuery, _kernel
        from tests.unit.test_browser_tree_containment import eventually
        module = self.module()
        location = Path(self.directory.name)
        identity = capture_owner_identity()
        creator = subprocess.Popen([sys.executable,
            str(Path("tests/fixtures/browser_tree_retained_creator.py").resolve()),
            str(location), sys.executable, json.dumps(identity)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        query = None
        try:
            eventually(lambda: (location / "retained.json").exists())
            transferred = json.loads((location / "retained.json").read_text())
            query = BrowserTreeQuery(_kernel(), transferred["handle"])
            self.observer.identity = identity
            metadata = self.row()["recovery_metadata"]
            metadata["manual_auth_owner"]["api_process"] = transferred["creator"]
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == self.operation)
                    .values(recovery_metadata=metadata))
            self.assertTrue(module.enroll_retained_observer(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            (location / "enrolled").touch()
            self.assertEqual(creator.wait(timeout=25), 0)
            self.assertEqual(owner_process_status(transferred["creator"]), "EXITED")
            self.assertEqual(query.status(), "LIVE")
            self.assertFalse(module.record_retained_closure(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            ready = json.loads((location / "ready.json").read_text())
            for pid in ready["members"]:
                self.assertTrue(query.contains(pid))
            (location / "release").touch()
            eventually(lambda: query.status() == "EXITED", 20)
            peer = Store(self.url)
            try:
                self.assertTrue(module.record_retained_closure(peer, self.operation, query,
                    self.binding, owner_token=self.token))
                certificate = self.row()["recovery_metadata"]["manual_auth_owner"]["tree_closure"]
                self.assertEqual(certificate["observer"], identity)
                self.assertEqual(certificate["accounting"]["active"], 0)
                self.assertEqual(certificate["accounting"]["terminated"], 0)
                self.assertNotIn("tree_closure", str(operation_public_record(self.row())))
                with self.assertRaises(OperationConcurrencyLimit):
                    admit_inline(peer, "MANUAL_AUTH", "another-app")
            finally:
                peer.engine.dispose()
        finally:
            (location / "enrolled").touch()
            (location / "release").touch()
            creator.wait(timeout=25)
            if query is not None:
                query.close()

    @unittest.skipUnless(sys.platform == "win32", "Native retained query provenance")
    def test_unrelated_query_and_ipc_enrollment_cannot_substitute_for_retained_query(self):
        from services.applications.browser_tree import BrowserTreeJob
        module = self.module()
        job, other_job = BrowserTreeJob(), BrowserTreeJob()
        query, other = job.query_handle(), other_job.query_handle()
        try:
            self.assertTrue(module.enroll_retained_observer(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            self.assertTrue(module.enroll_retained_observer(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            baseline = self.row()
            self.assertFalse(module.enroll_retained_observer(self.store, self.operation, other,
                self.binding, owner_token=self.token))
            with patch.object(other, "accounting", return_value={"total": 1, "active": 0, "terminated": 0}):
                self.assertFalse(module.record_retained_closure(self.store, self.operation, other,
                    self.binding, owner_token=self.token))
            self.assertEqual(self.row(), baseline)
            # The IPC channel cannot overwrite the retained authority either.
            from services.applications.browser_process import capture_owner_identity
            self.observer.identity = capture_owner_identity()
            self.assertFalse(self.persist())
        finally:
            query.close()
            other.close()
            job.close()
            other_job.close()
