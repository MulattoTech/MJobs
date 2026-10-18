"""MJ-REQ-008: abandoned Linux RQ execution -> durable outbox redelivery.

Only the explicitly configured disposable CI Redis is admitted. Natural expiry
and a new production worker must perform recovery; no registry/clock rewrites.
"""
from dataclasses import replace
from datetime import datetime
from contextlib import contextmanager
import asyncio
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient
import httpx
from redis import Redis
from rq import Queue, Worker
from rq.job import Job
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import create_mock_app, fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import read_attempt_history
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.dispatch import dispatch_pending

from tests.integration.rq_workhorse_fixture import (
    OwnedWorker, ProcessIdentity, admit_redis, process_identity, signal_owned, until,
)


class WorkhorseFixtureAdmissionTests(unittest.TestCase):
    @contextmanager
    def signals(self):
        with patch("os.pidfd_open", create=True, return_value=987), patch(
                "signal.pidfd_send_signal", create=True) as send, patch("os.close") as close:
            yield send
        close.assert_called_once_with(987)

    def test_only_explicit_disposable_ci_redis_is_admitted(self):
        self.assertEqual(admit_redis("redis://127.0.0.1:6379/0"),
                         "redis://127.0.0.1:6379/0")
        for target in (None, "", "redis://remote.invalid:6379/0",
                       "redis://127.0.0.1:6379/1", "redis://127.0.0.1:6380/0",
                       "redis://user:secret@127.0.0.1:6379/0",
                       "redis://127.0.0.1:6379/0?db=0",
                       "redis://127.0.0.1:6379/0#fragment"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                admit_redis(target)

    def test_linux_stat_parser_handles_spaces_and_parentheses_in_process_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            fields = ["S", "100", "100", "100"] + ["0"] * 15 + ["87654"]
            (root / "123" / "stat").write_text("123 (worker (fixture)) " + " ".join(fields))
            self.assertEqual(process_identity(123, proc=root),
                             ProcessIdentity(123, 100, 100, 100, 87654))

    def test_reused_pid_or_changed_session_is_never_signalled(self):
        owned = ProcessIdentity(123, 100, 100, 100, 87654)
        for changed in (replace(owned, started=87655), replace(owned, session=999),
                        replace(owned, group=999)):
            with self.subTest(changed=changed), patch(
                    "tests.integration.rq_workhorse_fixture.process_identity", return_value=changed), self.signals() as kill:
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    signal_owned(owned, signal.SIGTERM)
                kill.assert_not_called()

    def test_orphaned_owned_horse_retains_identity_but_missing_process_is_ignored(self):
        owned = ProcessIdentity(123, 100, 100, 100, 87654)
        with patch("tests.integration.rq_workhorse_fixture.process_identity",
                   return_value=replace(owned, parent=1)), self.signals() as kill:
            self.assertTrue(signal_owned(owned, signal.SIGTERM))
            kill.assert_called_once_with(987, signal.SIGTERM)
        with patch("tests.integration.rq_workhorse_fixture.process_identity",
                   side_effect=FileNotFoundError), self.signals() as kill:
            self.assertFalse(signal_owned(owned, signal.SIGTERM))
            kill.assert_not_called()

    def test_public_admission_replay_cancel_and_fresh_readback_need_no_redis(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-rq-admission-") as directory:
            root = Path(directory)
            url = f"sqlite:///{(root / 'admission.db').as_posix()}"
            store = Store(url)
            reopened = Store(url)
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                with patch("workers.dispatch.Redis.from_url", side_effect=AssertionError(
                        "Public admission must persist before queue I/O")):
                    with TestClient(create_app(store, "http://127.0.0.1:1", root,
                                              operation_mode="FIXTURE")) as client:
                        self.assertEqual(client.put("/profiles", json=PROFILE).status_code, 200)
                        payload = {"profile_id": PROFILE["id"], "request_id": uuid4().hex}
                        response = client.post("/discovery", json=payload)
                        self.assertEqual(response.status_code, 202, response.text)
                        identifier = response.json()["operation_id"]
                        self.assertEqual(client.post("/discovery", json=payload).json()["operation_id"], identifier)
                        cancelled = client.post(f"/operations/{identifier}/cancel")
                        self.assertEqual(cancelled.status_code, 202, cancelled.text)
                        self.assertEqual(cancelled.json()["state"], "CANCELLED")
                    with TestClient(create_app(reopened, "http://127.0.0.1:1", root,
                                              operation_mode="FIXTURE")) as fresh:
                        saved = fresh.get(f"/operations/{identifier}").json()
                        self.assertEqual((saved["state"], saved["attempt_count"]), ("CANCELLED", 0))
                        replay = fresh.post("/discovery", json=payload)
                        self.assertEqual(replay.json(), {"operation_id": identifier, "state": "CANCELLED"})
                self.assertEqual(reopened.count("worker_operations"), 1)
            finally:
                reopened.engine.dispose()
                store.engine.dispose()

    def test_horse_binding_accepts_rq_new_group_and_rejects_unowned_ancestry(self):
        fixture = OwnedWorker(None, None, "redis://127.0.0.1:6379/0", "http://127.0.0.1:1", Path("."), "guard")
        fixture.process = SimpleNamespace(pid=100)
        worker = SimpleNamespace(name="owned-worker", get_current_job_id=lambda: "owned-job")
        execution = SimpleNamespace(worker_name="owned-worker")
        job = SimpleNamespace(id="owned-job", worker_name="owned-worker", refresh=lambda: None,
                              get_status=lambda **_kwargs: "started", get_executions=lambda: [execution])
        horse = ProcessIdentity(123, 100, 123, 100, 87654)
        with patch.object(fixture, "registered", return_value=worker), patch(
                "pathlib.Path.read_text", return_value="123"), patch(
                "tests.integration.rq_workhorse_fixture.descendants", return_value=[]):
            with patch("tests.integration.rq_workhorse_fixture.process_identity", return_value=horse):
                self.assertIs(fixture.capture_horse(job), execution)
                self.assertEqual(fixture.horse, horse)
            for changed in (replace(horse, parent=999), replace(horse, group=999),
                            replace(horse, session=999)):
                with self.subTest(changed=changed), patch(
                        "tests.integration.rq_workhorse_fixture.process_identity", return_value=changed):
                    with self.assertRaisesRegex(AssertionError, "ancestry/session"):
                        fixture.capture_horse(job)

    def test_busy_worker_failure_before_horse_capture_stops_all_owned_processes(self):
        self.cleanup_scenario()

    def test_horse_exits_between_inventory_and_freeze_without_losing_live_orphan(self):
        for terminal in ("Z", "gone"):
            with self.subTest(terminal=terminal):
                self.cleanup_scenario(horse_exit=terminal)

    def cleanup_scenario(self, *, horse_exit=None):
        fixture = OwnedWorker(None, None, "redis://127.0.0.1:6379/0", "http://127.0.0.1:1", Path("."), "failure")
        parent = ProcessIdentity(100, 50, 100, 100, 1000)
        horse = ProcessIdentity(123, 100, 123, 100, 1001)
        browser = ProcessIdentity(234, 123, 123, 100, 1002)
        live = {100: parent, 123: horse, 234: browser}
        sent = []

        def send(owned, sig):
            sent.append((owned.pid, sig))
            if sig == 9:
                live.pop(owned.pid, None)
            elif sig == 19:
                if owned.pid == 123 and horse_exit:
                    if horse_exit == "gone":
                        live.pop(123)
                    else:
                        live[123] = replace(live[123], state=horse_exit)
                    # A child can outlive its terminal horse. Preserve its
                    # identity even though it is now outside the root's tree.
                    live[234] = replace(live[234], parent=1)
                    live[345] = ProcessIdentity(345, 234, 123, 100, 1003)
                else:
                    live[owned.pid] = replace(live[owned.pid], state="T")
            return True

        def wait(**_kwargs):
            if 100 in live:
                raise subprocess.TimeoutExpired("synthetic owned worker", 5)
            return -9

        def identity(pid):
            if pid not in live:
                raise FileNotFoundError
            return live[pid]

        def inventory(pid):
            found = [owned for owned in live.values() if owned.parent == pid]
            for child in list(found):
                found.extend(inventory(child.pid))
            return found

        fixture.identity, fixture.log = parent, Mock()
        fixture.process = SimpleNamespace(pid=100, poll=lambda: None if 100 in live else -9, wait=wait)
        fixture.worker = Mock()
        # Simulates either an early assertion failure or a busy replacement:
        # capture_horse() has never run, so both fields are initially empty.
        self.assertIsNone(fixture.horse)
        self.assertEqual(fixture.children, [])
        with patch("signal.SIGKILL", 9, create=True), patch("signal.SIGSTOP", 19, create=True), patch(
                "tests.integration.rq_workhorse_fixture.signal_owned", side_effect=send), patch(
                "tests.integration.rq_workhorse_fixture.descendants", side_effect=inventory), patch(
                "tests.integration.rq_workhorse_fixture.process_identity", side_effect=identity):
            fixture.__exit__(AssertionError, AssertionError("synthetic early failure"), None)
            fixture.assert_stopped()
        self.assertEqual(live, {}, "Failure cleanup leaked an uncaptured workhorse/browser")
        self.assertTrue({123, 234}.issubset({pid for pid, sig in sent if sig == 9}))
        fixture.worker.register_death.assert_called_once()
        fixture.log.close.assert_called_once()

    def test_unverified_or_live_cleanup_cannot_authorize_fixture_deletion(self):
        fixture = OwnedWorker(None, None, "redis://127.0.0.1:6379/0", "http://127.0.0.1:1", Path("."), "failure")
        with self.assertRaisesRegex(RuntimeError, "preserve fixture"):
            fixture.assert_stopped()
        case = RqWorkhorseRecoveryTests("test_abandoned_execution_maintenance_redelivers_to_fresh_production_workhorse")
        case.workers, case.operation_ids, case.root = [fixture], {"owned-job"}, Path("unused-fixture")
        with patch("rq.job.Job.exists") as exists, patch("shutil.rmtree") as remove:
            with self.assertRaisesRegex(RuntimeError, "preserve fixture"):
                case.delete_owned_jobs()
            with self.assertRaisesRegex(RuntimeError, "preserve fixture"):
                case.delete_owned_directory()
            exists.assert_not_called()
            remove.assert_not_called()
        fixture.cleanup_verified = True
        fixture.remember([ProcessIdentity(100, 50, 100, 100, 1000)])
        with patch("tests.integration.rq_workhorse_fixture.running", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "preserve fixture"):
                fixture.assert_stopped()


@unittest.skipUnless(sys.platform == "linux", "NOT_RUN: real forking RQ worker requires Linux")
@unittest.skipUnless(os.environ.get("MJOBS_REDIS_URL"), "NOT_RUN: explicit disposable CI Redis absent")
class RqWorkhorseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.redis_url = admit_redis(os.environ["MJOBS_REDIS_URL"])
        self.redis = Redis.from_url(self.redis_url, socket_connect_timeout=2, socket_timeout=2)
        self.addCleanup(self.redis.close)
        self.redis.ping()  # Configured-but-unavailable is a failure, never a skip.
        self.queue = Queue("mjobs-discovery", connection=self.redis)
        for queue_name in ("mjobs-discovery", "mjobs-application"):
            queue = Queue(queue_name, connection=self.redis)
            self.assertEqual(queue.get_job_ids(), [], "Refusing preexisting queued work")
            for registry in (StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry):
                self.assertEqual(registry(queue_name, connection=self.redis).get_job_ids(
                    cleanup=False), [], "Refusing preexisting active or scheduled work")
            self.assertEqual(self.redis.llen(queue.intermediate_queue_key), 0)
        self.assertFalse([worker for worker in Worker.all(connection=self.redis)
                          if set(worker.queue_names()) & {"mjobs-discovery", "mjobs-application"}],
                         "Refusing another worker's queue")
        self.workers = []
        self.root = Path(tempfile.mkdtemp(prefix="mjobs-workhorse-"))
        self.addCleanup(self.delete_owned_directory)
        self.store = Store(f"sqlite:///{(self.root / 'operation.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.operation_ids = set()
        self.addCleanup(self.delete_owned_jobs)

    def delete_owned_jobs(self):
        self.assert_workers_stopped()
        for identifier in self.operation_ids:
            if Job.exists(identifier, connection=self.redis):
                Job.fetch(identifier, connection=self.redis).delete()

    def assert_workers_stopped(self):
        for worker in self.workers:
            worker.assert_stopped()

    def delete_owned_directory(self):
        self.assert_workers_stopped()
        shutil.rmtree(self.root)

    def worker(self, origin, suffix, *, queue="mjobs-discovery"):
        worker = OwnedWorker(self.store, self.redis, self.redis_url, origin, self.root, suffix, queue=queue)
        self.workers.append(worker)
        return worker

    def read(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == identifier)).mappings().one())

    def client(self, store, origin):
        return TestClient(create_app(store, origin, self.root, self.redis_url,
                                     operation_mode="FIXTURE"))

    def admit(self, client):
        profile = {**PROFILE, "id": "rq-horse-" + uuid4().hex}
        response = client.put("/profiles", json=profile)
        self.assertEqual(response.status_code, 200, response.text)
        payload = {"profile_id": profile["id"], "request_id": uuid4().hex}
        response = client.post("/discovery", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["operation_id"]
        self.operation_ids.add(identifier)
        self.assertEqual(client.get(f"/operations/{identifier}").json()["state"], "QUEUED")
        return identifier, payload

    def test_abandoned_execution_maintenance_redelivers_to_fresh_production_workhorse(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        app = create_mock_app()
        board_requests = []

        @app.middleware("http")
        async def hold_first_source_response(request, call_next):
            response = await call_next(request)
            if request.url.path != "/board/jobs":
                return response
            board_requests.append(request.url.path)
            if len(board_requests) > 1:
                return response

            async def interrupted_body():
                entered.set()
                # Real socket reads remain active while the test identifies the
                # owned horse. Whitespace is legal JSON; the payload is withheld.
                deadline = time.monotonic() + 25
                while not release.is_set() and time.monotonic() < deadline:
                    yield b" "
                    await asyncio.sleep(.1)
                async for chunk in response.body_iterator:
                    yield chunk

            return StreamingResponse(interrupted_body(), media_type="application/json")

        with patch("fixtures.mock_server.create_mock_app", return_value=app), fixture_server() as origin:
            with self.client(self.store, origin) as client:
                identifier, payload = self.admit(client)
                self.assertEqual(dispatch_pending(self.store, self.redis_url)["count"], 1)
                job = Job.fetch(identifier, connection=self.redis)
                self.assertEqual(job.func_name, "workers.tasks.execute_operation")
                self.assertEqual(job.args, (identifier,))
                with self.worker(origin, "first") as first:
                    until(entered.is_set, "Production workhorse never reached the loopback source")
                    execution = until(lambda: first.capture_horse(job), "Workhorse identity was not established")
                    original = self.read(identifier)
                    self.assertEqual((original["state"], original["attempt_count"]), ("RUNNING", 1))
                    registry_score = self.redis.zscore(self.queue.started_job_registry.key, execution.composite_key)
                    self.assertIsNotNone(registry_score)
                    self.assertLessEqual(registry_score, time.time() + 95, "RQ expiry exceeds the bounded fixture window")
                    # A second actual worker must not turn a live started job
                    # into a second delivery, claim or source request.
                    with self.worker(origin, "live-control") as live:
                        until(live.registered, "Live-owner control worker did not register")
                        self.assertEqual(dispatch_pending(self.store, self.redis_url)["count"], 0)
                        self.assertEqual(self.read(identifier)["attempt_count"], 1)
                        self.assertEqual(job.get_status(refresh=True), "started")
                        self.assertEqual(len(board_requests), 1)
                    first.abandon()
                    self.assertEqual(first.process.returncode, -signal.SIGKILL)
                    self.assertEqual(job.get_status(refresh=True), "started")
                    self.assertEqual(self.read(identifier)["state"], "RUNNING")
                    release.set()

                    # Natural SQL expiry alone is insufficient: the real
                    # started registry still owns delivery until maintenance.
                    lease = datetime.fromisoformat(original["lease_expires_at"]).timestamp()
                    until(lambda: time.time() > lease + .2, "SQL lease did not expire", seconds=70)
                    self.assertEqual(dispatch_pending(self.store, self.redis_url)["count"], 0)
                    self.assertEqual(self.read(identifier)["state"], "QUEUED")
                    self.assertEqual(job.get_status(refresh=True), "started")
                    self.assertLess(time.time(), registry_score, "Fixture lost the SQL/RQ expiry gap")
                    until(lambda: time.time() > registry_score + 1,
                          "RQ execution did not naturally expire", seconds=65)

                    with self.worker(origin, "replacement") as replacement:
                        until(replacement.registered, "Replacement worker never registered")
                        until(lambda: self.read(identifier)["state"] == "COMPLETED"
                              and job.get_status(refresh=True) == "finished",
                              "Maintenance/outbox/replacement did not complete", seconds=30)
                        job.refresh()
                        self.assertNotEqual(first.worker.name, replacement.worker.name)
                        self.assertEqual(job.worker_name, replacement.worker.name)
                        self.assertEqual(self.read(identifier)["attempt_count"], 2)
                        self.assertEqual(dispatch_pending(self.store, self.redis_url)["count"], 0)
                    # This persisted child log distinguishes registry
                    # maintenance from ordinary queue delivery/parent handling.
                    lines = replacement.log_path.read_text().splitlines()
                    self.assertTrue(any(identifier in line and "AbandonedJobError" in line for line in lines))

                reopened = Store(self.store.url)
                try:
                    with self.client(reopened, origin) as fresh:
                        final = fresh.get(f"/operations/{identifier}").json()
                        self.assertEqual((final["state"], final["attempt_count"]), ("COMPLETED", 2))
                        replay = fresh.post("/discovery", json=payload)
                        self.assertEqual(replay.status_code, 202, replay.text)
                        self.assertEqual(replay.json()["operation_id"], identifier)
                        self.assertEqual(replay.json()["state"], "COMPLETED")
                    self.assertEqual(reopened.count("worker_operations"), 1)
                    self.assertEqual(len(reopened.jobs(payload["profile_id"])), 2)
                    with reopened.engine.connect() as connection:
                        effects = connection.execute(select(m.audit.c.id).where(
                            m.audit.c.action == "JOB_DISCOVERED")).all()
                    self.assertEqual(len(effects), 2, "One durable discovery effect per fixture job")
                    self.assertEqual(self.queue.get_job_ids(), [])
                    self.assertEqual(self.queue.started_job_registry.get_job_ids(cleanup=False), [])
                finally:
                    reopened.engine.dispose()

    def test_abandoned_mock_submit_with_unavailable_receipt_remains_uncertain_without_resubmission(self):
        accepted, release, receipt_outage = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        receipt_reads = []
        receipt_file = self.root / "synthetic-receipts.json"
        app = create_mock_app(receipt_file)

        @app.middleware("http")
        async def interrupt_accepted_submission(request, call_next):
            if receipt_outage.is_set() and request.url.path.startswith("/ats/receipts/"):
                receipt_reads.append(request.url.path)
                return Response(status_code=503, content=b"synthetic receipt outage")
            response = await call_next(request)
            if request.method != "POST" or not request.url.path.startswith("/ats/submit/"):
                return response
            self.assertEqual(response.status_code, 200)
            # The actual fixture handler has persisted one receipt; the browser
            # has not received the confirmation. No production hook is patched.
            accepted.set()

            async def withheld_confirmation():
                deadline = time.monotonic() + 25
                while not release.is_set() and time.monotonic() < deadline:
                    yield b" "
                    await asyncio.sleep(.1)
                async for chunk in response.body_iterator:
                    yield chunk

            return StreamingResponse(withheld_confirmation(), media_type="text/html")

        with patch("fixtures.mock_server.create_mock_app", return_value=app), fixture_server() as origin:
            # Controlled setup only. The operation under test is admitted by the
            # public mock-run API and executed solely by real RQ workhorses.
            self.store.save_profile(dict(PROFILE))
            discover(self.store, origin, PROFILE["id"])
            application = prepare(self.store, self.store.jobs(PROFILE["id"])[0]["id"],
                                  PROFILE["id"], self.root)
            endpoint = f"/profiles/{PROFILE['id']}/applications/{application}"
            request_id = uuid4().hex
            with self.client(self.store, origin) as client:
                queued = client.post(endpoint + "/mock-run", json={"submit": True, "request_id": request_id})
                self.assertEqual(queued.status_code, 202, queued.text)
                identifier = queued.json()["operation_id"]
                self.operation_ids.add(identifier)
                self.assertFalse(queued.json()["external_submission_enabled"])
                self.assertEqual(dispatch_pending(self.store, self.redis_url)["count"], 1)
                job = Job.fetch(identifier, connection=self.redis)
                self.assertEqual(job.origin, "mjobs-application")
                self.assertEqual(job.func_name, "workers.tasks.execute_operation")
                self.assertEqual(job.args, (identifier,))
                with self.worker(origin, "submit-first", queue="mjobs-application") as first:
                    until(accepted.is_set, "Real fixture browser never submitted to the loopback ATS", seconds=25)
                    execution = until(lambda: first.capture_horse(job), "Submission horse identity not established")
                    receipts = json.loads(receipt_file.read_text())
                    self.assertEqual(receipts[application]["submission_count"], 1)
                    self.assertEqual(self.store.application(application)["state"], "SUBMITTING")
                    original = self.read(identifier)
                    self.assertEqual((original["state"], original["attempt_count"]), ("RUNNING", 1))
                    attempts = read_attempt_history(self.store, PROFILE["id"], application)
                    self.assertEqual(len(attempts["attempts"]), 1)
                    score = self.redis.zscore(job.started_job_registry.key, execution.composite_key)
                    self.assertIsNotNone(score)
                    self.assertLessEqual(score, time.time() + 95)
                    first.abandon()
                    self.assertEqual(first.process.returncode, -signal.SIGKILL)
                    self.assertEqual(job.get_status(refresh=True), "started")
                    receipt_outage.set()
                    release.set()
                    # No SQL lease edits or fabricated Redis timestamps. The
                    # replacement performs its own outbox and RQ maintenance.
                    lease = datetime.fromisoformat(original["lease_expires_at"]).timestamp()
                    until(lambda: time.time() > max(lease, score) + 1,
                          "Submission execution did not naturally expire", seconds=100)
                    with self.worker(origin, "submit-replacement", queue="mjobs-application") as replacement:
                        until(replacement.registered, "Submission replacement worker did not register")
                        until(lambda: self.read(identifier)["state"] == "BLOCKED"
                              and job.get_status(refresh=True) == "finished",
                              "Ambiguous submission did not settle as blocked", seconds=30)
                        job.refresh()
                        self.assertEqual(job.worker_name, replacement.worker.name)
                        self.assertNotEqual(first.worker.name, replacement.worker.name)
                        self.assertEqual(self.read(identifier)["attempt_count"], 2)
                        self.assertEqual(dispatch_pending(self.store, self.redis_url)["count"], 0)
                    self.assertTrue(any(identifier in line and "AbandonedJobError" in line
                                        for line in replacement.log_path.read_text().splitlines()))

                reopened = Store(self.store.url)
                try:
                    with self.client(reopened, origin) as fresh:
                        operation = fresh.get(f"/operations/{identifier}").json()
                        self.assertEqual((operation["state"], operation["error"]),
                                         ("BLOCKED", "SUBMISSION_UNCONFIRMED"))
                        saved = fresh.get(endpoint).json()
                        self.assertEqual(saved["state"], "SUBMISSION_UNCONFIRMED")
                        self.assertEqual(len(saved["attempts"]), 1)
                        self.assertEqual(read_attempt_history(reopened, PROFILE["id"], application), attempts)
                        replay = fresh.post(endpoint + "/mock-run", json={"submit": True, "request_id": request_id})
                        self.assertEqual(replay.status_code, 202, replay.text)
                        self.assertEqual(replay.json()["operation_id"], identifier)
                        refused = fresh.post(endpoint + "/mock-run", json={"submit": True, "request_id": uuid4().hex})
                        self.assertEqual(refused.status_code, 409, refused.text)
                        self.assertEqual(refused.json()["detail"]["code"], "RECONCILIATION_REQUIRED")
                    self.assertTrue(receipt_reads, "Replacement must try the real receipt endpoint")
                    self.assertEqual(json.loads(receipt_file.read_text()), receipts)
                    self.assertEqual(httpx.get(origin + "/_test/request-counts", timeout=3,
                                               trust_env=False).json(), {"review": 1, "submit": 1})
                    self.assertEqual(read_attempt_history(reopened, PROFILE["id"], application), attempts)
                    self.assertEqual(reopened.count("worker_operations"), 1)
                finally:
                    reopened.engine.dispose()
