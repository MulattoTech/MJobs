"""Configured limits govern durable admission without performing any submission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, update

from fixtures.mock_server import fixture_server
from packages.domain.safety import Policy
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_adapter import _authorize
from services.applications.submission_admission import authorize_submission
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


KEYS = ("MJOBS_SUBMISSION_HOURLY_LIMIT", "MJOBS_SUBMISSION_DAILY_LIMIT",
        "MJOBS_SUBMISSION_COMPANY_LIMIT")


class ConfiguredSubmissionLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-submission-caps-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'caps.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.applications = []
        for job in self.store.jobs("fixture")[:2]:
            identifier = prepare(self.store, job["id"], "fixture", self.root)
            self.store.transition(identifier, ["PREPARED"], "READY_TO_SUBMIT")
            self.applications.append(identifier)
        self.assertEqual(len(self.applications), 2)

    def test_zero_each_cap_blocks_actual_mock_admission_without_claiming(self):
        for key, reason in zip(KEYS, ("HOURLY_LIMIT_REACHED", "DAILY_LIMIT_REACHED", "COMPANY_LIMIT_REACHED")):
            with self.subTest(setting=key), patch.dict(os.environ, {
                    **dict.fromkeys(KEYS, "10000"), key: "0"}):
                decision = _authorize(self.store, self.applications[0], 100)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reasons, (reason,))
                self.assertEqual(self.store.application(self.applications[0])["state"], "READY_TO_SUBMIT")
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action == "SUBMITTING")).scalar_one(), 0)

    def test_configured_hourly_cap_serializes_two_distinct_application_admissions(self):
        with patch.dict(os.environ, dict(zip(KEYS, ("1", "10000", "10000")))):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda identifier: authorize_submission(
                    self.store, identifier, 100), self.applications))
        self.assertEqual(sum(result.allowed for result in results), 1)
        denied = next(result for result in results if not result.allowed)
        self.assertEqual(denied.reasons, ("HOURLY_LIMIT_REACHED",))
        reopened = Store(self.store.url)
        try:
            states = [reopened.application(identifier)["state"] for identifier in self.applications]
            self.assertCountEqual(states, ["SUBMITTING", "READY_TO_SUBMIT"])
        finally:
            reopened.engine.dispose()

    def test_configured_caps_use_existing_rolling_and_company_durable_counters(self):
        first, second = (self.store.application(identifier) for identifier in self.applications)
        with self.store.engine.begin() as connection:
            company_id = connection.execute(select(m.jobs.c.company_id).where(
                m.jobs.c.id == first["job_id"])).scalar_one()
            connection.execute(update(m.jobs).where(m.jobs.c.id == second["job_id"]).values(
                company_id=company_id))
        for key, age, reason in (
                (KEYS[0], timedelta(minutes=30), "HOURLY_LIMIT_REACHED"),
                (KEYS[1], timedelta(hours=2), "DAILY_LIMIT_REACHED"),
                (KEYS[2], timedelta(days=2), "COMPANY_LIMIT_REACHED")):
            with self.subTest(setting=key):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.applications).where(
                        m.applications.c.id == first["id"]).values(
                            state="CONFIRMED", submitted_at=(datetime.now(timezone.utc) - age).isoformat()))
                with patch.dict(os.environ, {**dict.fromkeys(KEYS, "10000"), key: "1"}):
                    decision = authorize_submission(self.store, second["id"], 100)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reasons, (reason,))
                self.assertEqual(self.store.application(second["id"])["state"], "READY_TO_SUBMIT")

    def test_explicit_internal_policy_retains_existing_override_semantics(self):
        with patch.dict(os.environ, dict.fromkeys(KEYS, "0")):
            decision = authorize_submission(self.store, self.applications[0], 100, policy=Policy())
        self.assertTrue(decision.allowed)

    def test_configured_caps_cannot_enable_external_submission(self):
        identifier = self.applications[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == identifier).values(target="REAL"))
        with patch.dict(os.environ, dict.fromkeys(KEYS, "10000")):
            decision = authorize_submission(self.store, identifier, 100)
        self.assertFalse(decision.allowed)
        self.assertIn("EXTERNAL_SUBMISSION_DISABLED", decision.reasons)
        self.assertEqual(self.store.application(identifier)["state"], "READY_TO_SUBMIT")


if __name__ == "__main__":
    unittest.main()
