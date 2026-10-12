"""Opt-in durable extraction transaction/fence proof on disposable PostgreSQL."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_durable_resume_extraction as sqlite_contract


POSTGRES_URL = os.environ.get("MJOBS_DURABLE_EXTRACTION_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "Set MJOBS_DURABLE_EXTRACTION_POSTGRES_URL to a disposable loopback mjobs_test_* database")
class DurableResumeExtractionPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith("postgresql")
                or url.host not in ("localhost", "127.0.0.1", "::1")
                or not url.database or not url.database.startswith("mjobs_test_")):
            raise RuntimeError("A disposable loopback mjobs_test_* PostgreSQL database is required")
        self.schema = "mjobs_extract_" + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        scoped = url.update_query_dict({"options": "-csearch_path=" + self.schema + " -clock_timeout=5000 -cstatement_timeout=30000"})
        database_url = scoped.render_as_string(hide_password=False)

        class PostgreSQLTestStore(Store):
            def __init__(self, _ignored):
                super().__init__(database_url)

            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)

        self.case = sqlite_contract.DurableResumeExtractionTests()
        store_patch = patch.object(sqlite_contract, "Store", PostgreSQLTestStore)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.case.setUp()
        # Worker subprocesses read this URL directly, so they must share the
        # UUID-scoped PostgreSQL schema rather than the fixture's SQLite path.
        self.case.database_url = database_url
        self.addCleanup(self.case.tearDown)

    def cleanup_schema(self):
        if getattr(self, "admin", None) is not None:
            with self.admin.begin() as connection:
                connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
            self.admin.dispose()

    def test_same_intent_replay_and_terminal_retry_on_postgresql(self):
        self.case.test_terminal_intent_replays_unchanged_and_explicit_new_intent_can_retry()

    def test_result_and_operation_completion_are_atomic_on_postgresql(self):
        self.case.test_result_and_completed_state_roll_back_together_on_result_write_failure()

    def test_stale_worker_fence_prevents_postgres_result_publication(self):
        self.case.test_stale_worker_cannot_commit_after_expired_lease_is_reclaimed()

    def test_process_crash_after_extraction_replays_on_postgresql(self):
        self.case.test_process_crash_after_extraction_replays_and_commits_one_result()

    def test_http_disconnect_after_admission_leaves_postgres_operation_queued(self):
        self.case.test_real_http_disconnect_after_durable_admission_leaves_operation_queued()

    def test_original_api_admission_and_worker_readback_on_postgresql(self):
        self.case.test_original_extract_url_admits_replayable_work_without_api_parsing()

    def test_cross_instance_cancellation_reaps_native_parser_on_postgresql(self):
        self.case.test_cross_instance_cancel_reaps_running_native_parser_without_losing_upload()


if __name__ == "__main__":
    unittest.main()
