"""Refresh/reopen, rollback and concurrent intake on a disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from packages.persistence import models as m
from services.company_discovery.requests import record_discovery_request
from tests.integration import test_discovery_request_candidate_refresh as contract


POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "NOT_RUN: disposable loopback MJOBS_TEST_POSTGRES_URL absent")
class CandidateRefreshPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith("postgresql")
                or url.host not in {"localhost", "127.0.0.1", "::1"} or url.query
                or not url.database or not (url.database == "mjobs_test" or url.database.startswith("mjobs_test_"))):
            raise RuntimeError("Requires an explicitly supplied disposable loopback mjobs_test database")
        self.schema = "mjobs_candidate_" + uuid4().hex
        self.admin = create_engine(url, connect_args={"connect_timeout": 5})
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.drop_schema)
        scoped = url.update_query_dict({"options": "-csearch_path=" + self.schema
                                        + " -clock_timeout=5000 -cstatement_timeout=30000"})
        database_url = scoped.render_as_string(hide_password=False)
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": database_url}):
            command.upgrade(Config("alembic.ini"), "head")
        self.case = contract.CandidateRefreshTests()
        self.case.database_url = database_url
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))

    def test_refresh_revision_and_single_outbox_survive_reopen(self):
        self.case.test_later_bound_job_origin_queues_one_high_priority_operation_and_survives_reopen()

    def test_queue_failure_rolls_back_candidate_and_audit(self):
        self.case.test_failed_queue_rolls_back_candidate_and_refresh_audit()

    def test_concurrent_intakes_commit_one_refresh_and_one_operation(self):
        pending, job = self.case.staged_candidate()
        store = self.case.case.store
        profile = store.profile("fixture")
        start = threading.Barrier(2)
        def intake():
            with store.engine.begin() as connection:
                start.wait(timeout=5)
                return record_discovery_request(connection, job["id"], profile)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(intake) for _ in range(2)]
            self.assertEqual([future.result(timeout=10) for future in futures], [pending["id"]] * 2)
        with store.engine.connect() as connection:
            request = connection.execute(select(m.company_discovery_requests)).mappings().one()
            self.assertEqual(request["revision"], pending["revision"] + 1)
            self.assertEqual(request["trigger_job_id"], job["id"])
            self.assertEqual(request["state"], "QUEUED")
            operations = list(connection.execute(select(m.operations).where(
                m.operations.c.kind == "COMPANY_DISCOVER")).mappings())
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]["params"]["dispatch_priority"], "HIGH")
            self.assertEqual(operations[0]["id"], request["operation_id"])
            self.assertEqual(len(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action == "COMPANY_DISCOVERY_REQUEST_CANDIDATE_REFRESHED")).all()), 1)
            self.assertEqual(connection.execute(select(m.company_domains)).all(), [])
