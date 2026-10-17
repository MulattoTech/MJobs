"""Team 12 connected browser-session failure isolation and durable closure."""
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from threading import RLock
from unittest.mock import Mock, patch

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.browser_tree import BrowserTreeJob
from services.applications.browser_tree_ipc import BrowserTreeObserver
from services.operations.progress import operation_public_record
from tests.unit.test_browser_tree_containment import eventually
from workers.operations import OperationConcurrencyLimit, admit_inline


@unittest.skipUnless(sys.platform == "win32", "Native Windows contained process trees")
class FleetT12BrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t12-lifecycle-")
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'fixture.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.token = "a" * 64
        self.operation = admit_inline(self.store, "MANUAL_AUTH", "synthetic-app-1",
            owner_token=self.token,
            manual_auth_identity={"profile_id": "synthetic-profile-1",
                                  "attempt_id": "synthetic-attempt-1"})

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def row(self, store, operation):
        with store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation)).mappings().one())

    def test_observer_interruption_isolated_and_closure_persists_per_session(self):
        jobs = [BrowserTreeJob(), BrowserTreeJob()]
        queries = [job.query_handle() for job in jobs]
        bindings = [
            {"operation_id": self.operation, "application_id": "synthetic-app-1",
             "attempt_id": "synthetic-attempt-1", "profile_id": "synthetic-profile-1",
             "owner_digest": sha256(self.token.encode()).hexdigest()},
            {"operation_id": "independent-observation", "application_id": "independent-app",
             "attempt_id": "independent-attempt", "profile_id": "independent-profile",
             "owner_digest": "c" * 64},
        ]
        observers = []
        locations = [self.root / "session-one", self.root / "session-two"]
        try:
            observers = [BrowserTreeObserver(queries[0], bindings[0], store=self.store,
                                             owner_token=self.token),
                         BrowserTreeObserver(queries[1], bindings[1])]
            fixture = Path(__file__).resolve().parents[1] / "fixtures" / "browser_tree_child.py"
            for job, location in zip(jobs, locations):
                location.mkdir()
                job.launch([sys.executable, str(fixture), "root", str(location)], cwd=Path.cwd())
            for location in locations:
                eventually(lambda location=location: (location / "ready.json").exists())

            # Lose only session one's creator channel after sending an observe
            # request. Its outside observer must retain authority until closure.
            interrupted_at = time.monotonic()
            with patch("services.applications.browser_tree_ipc._read",
                       side_effect=TimeoutError("synthetic channel interruption")):
                self.assertEqual(observers[0].observe(bindings[0])["status"], "UNKNOWN")
            self.assertLess(time.monotonic() - interrupted_at, 5)

            self.assertEqual(observers[1].observe(bindings[1])["status"], "LIVE")
            self.assertEqual(queries[0].status(), "LIVE")
            self.assertNotIn("tree_closure",
                self.row(self.store, self.operation)["recovery_metadata"]["manual_auth_owner"])
            with self.assertRaises(OperationConcurrencyLimit):
                admit_inline(self.store, "MANUAL_AUTH", "synthetic-capacity-probe")

            # Cooperative release is fixture input, not production closure
            # evidence; require native tree accounting and persisted readback.
            (locations[0] / "release").touch()
            eventually(lambda: queries[0].status() == "EXITED")
            peer = Store(self.url)
            try:
                persisted = eventually(lambda: (
                    row if "tree_closure" in (row := self.row(peer, self.operation)
                        )["recovery_metadata"]["manual_auth_owner"] else None), 20)
                certificate = persisted["recovery_metadata"]["manual_auth_owner"]["tree_closure"]
                self.assertEqual(certificate["binding"], bindings[0])
                self.assertEqual(certificate["observer"], observers[0].identity)
                self.assertEqual(certificate["accounting"]["active"], 0)
                self.assertEqual(certificate["accounting"]["terminated"], 0)
                self.assertEqual(persisted["state"], "RUNNING")
                self.assertNotIn("tree_closure", str(operation_public_record(persisted)))
                self.assertEqual(observers[1].observe(bindings[1])["status"], "LIVE")
                with self.assertRaises(OperationConcurrencyLimit):
                    admit_inline(peer, "MANUAL_AUTH", "synthetic-capacity-probe-after-readback")
            finally:
                peer.engine.dispose()
        finally:
            for location in locations:
                location.mkdir(exist_ok=True)
                (location / "release").touch()
            for observer in observers:
                observer.close()
            for query in queries:
                query.close()
            for job in jobs:
                job.close()


class _Pipe:
    def __init__(self):
        self.closed = False

    def write(self, frame):
        if self.closed:
            raise BrokenPipeError("synthetic delayed observer reply")
        return len(frame)

    def flush(self):
        if self.closed:
            raise BrokenPipeError("synthetic delayed observer reply")

    def close(self):
        self.closed = True


class _LateObserverProcess:
    def __init__(self):
        self.stdin = _Pipe()
        self.stdout = _Pipe()
        self.wait_timeout = None

    def poll(self):
        return None

    def wait(self, timeout):
        self.wait_timeout = timeout
        if self.stdout.closed:
            raise AssertionError("stdout closed before the bounded wait")
        raise subprocess.TimeoutExpired("synthetic-observer", timeout)


class FleetT12ObserverPipeOrderingTests(unittest.TestCase):
    def observer(self, process):
        observer = object.__new__(BrowserTreeObserver)
        observer._lock = RLock()
        observer._process = process
        observer._closed = False
        observer._binding = {"operation_id": "late-op", "application_id": "late-app",
            "attempt_id": "late-attempt", "profile_id": "late-profile", "owner_digest": "d" * 64}
        observer.identity = {"pid": 1, "created": "1", "host": "e" * 64}
        return observer

    def test_delayed_response_after_close_wait_enters_retained_closure_path(self):
        from services.applications import browser_tree_ipc as ipc

        process = _LateObserverProcess()
        observer = self.observer(process)
        observer.close()
        self.assertEqual(process.wait_timeout, 2)
        self.assertTrue(process.stdout.closed)

        class Query:
            def accounting(self):
                return {"total": 2, "active": 1, "terminated": 0}

            def status(self):
                return "EXITED"

        persist = Mock(return_value=True)
        result = ipc._respond(process.stdout, "a" * 64, observer._binding,
            observer.identity, Query(), object(), persist)
        self.assertFalse(result)
        persist.assert_called_once_with()

    def test_unlaunched_response_loss_does_not_wait_or_persist(self):
        from services.applications import browser_tree_ipc as ipc

        process = _LateObserverProcess()
        observer = self.observer(process)
        observer.close()

        class Query:
            def accounting(self):
                return {"total": 0, "active": 0, "terminated": 0}

            def status(self):
                raise AssertionError("unlaunched trees must not enter retained wait")

        persist = Mock()
        result = ipc._respond(process.stdout, "a" * 64, observer._binding,
            observer.identity, Query(), object(), persist)
        self.assertFalse(result)
        persist.assert_not_called()
