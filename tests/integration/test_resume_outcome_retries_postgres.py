"""Real PostgreSQL retries must serialize without rewriting reported history."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text

from packages.persistence.resume_outcomes import bindings, events
from packages.persistence.store import Store, identity
from services.tracking.resume_outcomes import ResumeOutcomeConflict, record_observed_outcome
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
from tests.integration.test_spine import PROFILE


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database")
class ResumeOutcomeRetryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = _disposable_postgres_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        config = Config("alembic.ini")
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": cls.url.render_as_string(hide_password=False)}):
            command.upgrade(config, "head")

    def setUp(self):
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.profile_id = "retry-" + uuid4().hex
        self.store.save_profile({**PROFILE, "id": self.profile_id})
        self.application_id = "application-" + uuid4().hex
        self.binding = self._binding(self.application_id)
        self.other_application_id = "application-" + uuid4().hex
        self.other_binding = self._binding(self.other_application_id)
        with self.store.engine.begin() as connection:
            connection.execute(bindings.insert(), [self.binding, self.other_binding])

    def _binding(self, application_id):
        return {
            "id": identity("resume-outcome-binding", self.profile_id, application_id),
            "profile_id": self.profile_id, "application_id": application_id,
            "document_id": "reported-document-" + application_id,
            "document_version": 3, "document_sha256": {"resume": "synthetic-digest"},
            "reported_at": "2026-11-01T00:00:00+00:00", "request_id": "binding",
            "created_at": "2026-11-01T00:00:00+00:00",
        }

    def _race(self, retry_kind="INTERVIEW", retry_time="2026-11-03T00:00:00+00:00",
              unrelated=False):
        inserted, release, second_started = Event(), Event(), Event()
        pids = {}
        first_store, second_store = Store(self.url), Store(self.url)
        for store in (first_store, second_store):
            self.addCleanup(store.engine.dispose)

        def observe_connection(label):
            def before_execute(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("SELECT") and "FROM resume_outcome_bindings" in statement:
                    # Consume this diagnostic query before SQLAlchemy executes the real SELECT.
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[label] = cursor.fetchone()[0]
                    cursor.execute("SET LOCAL statement_timeout = '15s'")
                    if label == "second":
                        second_started.set()
            return before_execute

        def pause_after_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO resume_outcome_events"):
                inserted.set()
                if not release.wait(15):
                    raise AssertionError("Coordinator did not release the first transaction")

        event.listen(first_store.engine, "before_cursor_execute", observe_connection("first"))
        event.listen(second_store.engine, "before_cursor_execute", observe_connection("second"))
        event.listen(first_store.engine, "after_cursor_execute", pause_after_insert)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(record_observed_outcome, first_store, self.profile_id,
                                self.application_id, "INTERVIEW", "2026-11-03T00:00:00+00:00", "retry")
            try:
                self.assertTrue(inserted.wait(10), "First call did not insert its uncommitted event")
                application_id = self.other_application_id if unrelated else self.application_id
                second = pool.submit(record_observed_outcome, second_store, self.profile_id,
                                     application_id, retry_kind, retry_time, "retry")
                self.assertTrue(second_started.wait(10), "Second call did not reach its binding read")
                self.assertNotEqual(pids["first"], pids["second"])
                if unrelated:
                    # Must commit while the first binding's transaction is still held open.
                    second_result = second.result(timeout=10)
                    with self.store.engine.connect() as connection:
                        self.assertEqual(connection.execute(select(events.c.binding_id).where(
                            events.c.binding_id.in_([self.binding["id"], self.other_binding["id"]])
                        )).scalars().all(), [self.other_binding["id"]])
                else:
                    deadline = monotonic() + 10
                    with self.store.engine.connect() as connection:
                        while monotonic() < deadline:
                            blockers = connection.execute(text("SELECT pg_blocking_pids(:pid)"),
                                                          {"pid": pids["second"]}).scalar_one()
                            if pids["first"] in blockers:
                                break
                            if second.done():
                                self.fail("Retry completed before the first transaction committed")
                            release.wait(0.01)
                        else:
                            self.fail("PostgreSQL did not report the expected transaction blocking")
                release.set()
                first_result = first.result(timeout=10)
                if not unrelated:
                    try:
                        second_result = second.result(timeout=10)
                    except ResumeOutcomeConflict as error:
                        second_result = error
                return first_result, second_result
            finally:
                release.set()

    def _assert_durable_history(self, other=False):
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(events).where(
                events.c.binding_id == self.binding["id"])).mappings().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "INTERVIEW")
            self.assertEqual(rows[0]["observed_at"], "2026-11-03T00:00:00+00:00")
            self.assertEqual(rows[0]["request_id"], "retry")
            for original in (self.binding, self.other_binding):
                self.assertEqual(dict(connection.execute(select(bindings).where(
                    bindings.c.id == original["id"])).mappings().one()), original)
            other_rows = connection.execute(select(events).where(
                events.c.binding_id == self.other_binding["id"])).mappings().all()
            self.assertEqual(len(other_rows), int(other))

    def test_concurrent_identical_retries_return_one_observation(self):
        first, second = self._race()
        self.assertEqual(first, second)
        self.assertEqual(first["document_id"], self.binding["document_id"])
        self._assert_durable_history()

    def test_concurrent_changed_kind_returns_documented_conflict(self):
        _, second = self._race(retry_kind="REJECTED")
        self.assertIsInstance(second, ResumeOutcomeConflict)
        self._assert_durable_history()

    def test_concurrent_changed_time_returns_documented_conflict(self):
        _, second = self._race(retry_time="2026-11-04T00:00:00+00:00")
        self.assertIsInstance(second, ResumeOutcomeConflict)
        self._assert_durable_history()

    def test_unrelated_binding_commits_while_first_transaction_is_open(self):
        first, second = self._race(unrelated=True)
        self.assertNotEqual(first["document_id"], second["document_id"])
        self._assert_durable_history(other=True)
