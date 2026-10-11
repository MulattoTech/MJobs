"""Transient worker failures survive restart and respect durable retry deadlines."""
from datetime import datetime, timedelta, timezone
from functools import partial
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source, collect_source
from services.discovery.sources.greenhouse import SourceError
from services.operations.progress import get_operation_progress
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation, _pending_operations
from workers.operations import claim, request_cancel
from workers.tasks import execute_operation


class OperationRetryTests(unittest.TestCase):
    def database_url(self):
        return f"sqlite:///{(self.root / 'retry.db').as_posix()}"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.url = self.database_url()
        self.store = Store(self.url)
        if self.url.startswith("sqlite:"):
            self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)
        self.source = configure_source(self.store, "synthetic-retry-" + uuid4().hex)
        self.environment = patch.dict(os.environ, {
            "MJOBS_DATABASE_URL": self.url, "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_OUTPUT_DIR": str(self.root), "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:8090",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.store.engine.dispose()
        self.directory.cleanup()

    def enqueue(self):
        return enqueue_operation(self.store, "DISCOVER", "fixture", {"source_id": self.source})

    def row(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one())

    def make_due(self, identifier):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                next_attempt_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))

    def run_failure(self, identifier, failure):
        # Only the provider boundary is replaced. Real worker claim, error
        # classification, database completion, audit and outbox run normally.
        with patch("workers.tasks.collect_source", side_effect=failure):
            return execute_operation(identifier)

    def test_transient_failure_persists_backoff_and_completes_same_operation_after_restart(self):
        identifier = self.enqueue()
        before = datetime.now(timezone.utc)
        self.run_failure(identifier, httpx.ReadTimeout("private-provider-message"))
        row = self.row(identifier)
        self.assertEqual(row["state"], "QUEUED")
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(row["attempt_count"], 1)
        self.assertIsNone(row["completed_at"])
        self.assertEqual(get_operation_progress(self.store, identifier)["next_attempt_at"], row["next_attempt_at"])
        self.assertGreaterEqual(datetime.fromisoformat(row["next_attempt_at"]), before + timedelta(seconds=60))
        self.assertNotIn("private-provider-message", str(row))
        self.assertIsNone(claim(self.store, identifier))
        with self.store.engine.connect() as connection:
            self.assertEqual(_pending_operations(connection), [])
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.assertEqual(self.row(identifier)["next_attempt_at"], row["next_attempt_at"])
        self.make_due(identifier)
        with patch("workers.tasks.collect_source", return_value={"jobs_seen": 0}):
            result = execute_operation(identifier)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.row(identifier)["attempt_count"], 2)
        self.assertIsNone(self.row(identifier)["next_attempt_at"])

    def test_real_source_timeout_policy_survives_restart_and_collects_once_on_retry(self):
        self.source = configure_source(self.store, "retry-remotive-" + uuid4().hex, provider="remotive")
        identifier = self.enqueue()
        requests = []
        def provider(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            if len(requests) == 1:
                raise httpx.ReadTimeout("private transport detail", request=request)
            return httpx.Response(200, json={"jobs": [], "job-count": 0})
        collector = partial(collect_source, transport=httpx.MockTransport(provider))
        with patch("workers.tasks.collect_source", collector):
            self.assertEqual(execute_operation(identifier)["status"], "QUEUED")
            row = self.row(identifier)
            with self.store.engine.connect() as connection:
                source = connection.execute(select(m.job_sources).where(m.job_sources.c.id == self.source)).mappings().one()
            self.assertGreaterEqual(datetime.fromisoformat(row["next_attempt_at"]),
                datetime.fromisoformat(source["last_attempt"]) + timedelta(seconds=source["poll_interval"]))
            self.assertGreaterEqual(row["next_attempt_at"], source["backoff_until"])
            self.assertEqual(len(requests), 1)
            self.assertIsNone(claim(self.store, identifier))
            self.store.engine.dispose(); self.store = Store(self.url)
            # Advance persisted policy timestamps in this controlled test;
            # ordinary dispatch cannot bypass either deadline.
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            with self.store.engine.begin() as connection:
                connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source).values(
                    last_attempt=past, backoff_until=past))
            self.make_due(identifier)
            self.assertEqual(execute_operation(identifier)["status"], "COMPLETED")
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.row(identifier)["attempt_count"], 2)
        self.assertEqual(self.store.count("canonical_jobs"), 0)

    def test_retry_limit_and_exponential_delay_are_bounded_without_reopening_terminal_work(self):
        identifier = self.enqueue()
        for attempt, delay in ((1, 60), (2, 120)):
            before = datetime.now(timezone.utc)
            self.run_failure(identifier, SourceError("SOURCE_HTTP_503"))
            row = self.row(identifier)
            self.assertEqual(row["state"], "QUEUED")
            self.assertEqual(row["attempt_count"], attempt)
            self.assertGreaterEqual(datetime.fromisoformat(row["next_attempt_at"]), before + timedelta(seconds=delay))
            self.make_due(identifier)
        self.run_failure(identifier, SourceError("SOURCE_HTTP_503"))
        row = self.row(identifier)
        self.assertEqual(row["state"], "DEAD_LETTERED")
        self.assertEqual(row["attempt_count"], 3)
        self.assertEqual(row["retry_count"], 2)
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNone(claim(self.store, identifier))
        self.assertEqual(enqueue_operation(self.store, "DISCOVER", "fixture", {"source_id": self.source}), identifier)
        self.assertEqual(self.row(identifier)["state"], "DEAD_LETTERED")

    def test_provider_backoff_and_poll_interval_prevent_an_early_retry(self):
        identifier = self.enqueue()
        provider_due = datetime.now(timezone.utc) + timedelta(hours=2)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source).values(
                backoff_until=provider_due.isoformat(), last_attempt=datetime.now(timezone.utc).isoformat()))
        self.run_failure(identifier, SourceError("SOURCE_RATE_LIMITED", retry_seconds=3600))
        self.assertGreaterEqual(datetime.fromisoformat(self.row(identifier)["next_attempt_at"]), provider_due)
        self.assertIsNone(claim(self.store, identifier))

    def test_cancellation_during_retry_wait_is_terminal(self):
        identifier = self.enqueue()
        self.run_failure(identifier, httpx.ConnectError("private"))
        self.assertEqual(self.row(identifier)["state"], "QUEUED")
        request_cancel(self.store, identifier)
        self.assertIsNone(self.row(identifier)["next_attempt_at"])
        self.assertIsNone(claim(self.store, identifier))
        self.assertEqual(self.row(identifier)["state"], "CANCELLED")

    def test_cancellation_during_provider_failure_cannot_schedule_a_retry(self):
        identifier = self.enqueue()

        def cancelled_provider(*args, **kwargs):
            request_cancel(self.store, identifier)
            raise httpx.ReadTimeout("private")

        with patch("workers.tasks.collect_source", side_effect=cancelled_provider):
            execute_operation(identifier)
        row = self.row(identifier)
        self.assertEqual(row["state"], "CANCELLED")
        self.assertEqual(row["retry_count"], 0)
        self.assertIsNone(row["next_attempt_at"])

    def test_nontransient_failures_never_schedule_automatic_retry(self):
        for code in ("AUTH_REQUIRED", "CAPTCHA_REQUIRED", "JOB_CLOSED",
                     "UNKNOWN_MANDATORY_QUESTION", "SUBMISSION_UNCONFIRMED", "SOURCE_HTTP_403"):
            with self.subTest(code=code):
                identifier = enqueue_operation(self.store, "DISCOVER", "fixture", {
                    "source_id": self.source, "negative_control": code})
                self.run_failure(identifier, SourceError(code))
                row = self.row(identifier)
                self.assertIn(row["state"], {"BLOCKED", "DEAD_LETTERED"})
                self.assertEqual(row["retry_count"], 0)
                self.assertIsNone(row["next_attempt_at"])


@unittest.skipUnless(os.environ.get("MJOBS_RETRY_POSTGRES_URL"), "Set MJOBS_RETRY_POSTGRES_URL to an isolated disposable PostgreSQL database")
class PostgreSQLOperationRetryTests(OperationRetryTests):
    @classmethod
    def setUpClass(cls):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.engine import make_url

        url = os.environ["MJOBS_RETRY_POSTGRES_URL"]
        target = make_url(url)
        if target.database != "mjobs_retry_validation" or target.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Retry acceptance requires isolated loopback database mjobs_retry_validation")
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
            command.upgrade(config, "head")

    def database_url(self):
        return os.environ["MJOBS_RETRY_POSTGRES_URL"]


if __name__ == "__main__":
    unittest.main()

