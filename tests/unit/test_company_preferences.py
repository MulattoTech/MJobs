"""Durable profile-scoped company preference decisions."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from services.matching.company_preferences import (
    CompanyPreferenceConflict,
    get_company_preference,
    list_company_preferences,
    set_company_preference,
)
from apps.api.application_priority import rank_jobs_by_application_priority
from tests.integration.test_spine import PROFILE


class CompanyPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'preferences.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        self.job = capture_manual_job(self.store, {
            "request_id": "company-preference-job",
            "title": "Systems Administrator",
            "company": "Northwind Systems",
            "description": "Manage Linux infrastructure.",
            "reference_url": "https://jobs.example.invalid/company-preference",
            "application_url": "https://apply.example.invalid/company-preference",
            "profile_id": "fixture",
        })["job"]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_preferences_default_to_neutral_and_are_isolated_by_profile(self):
        initial = get_company_preference(self.store, "fixture", self.job["company_id"])
        self.assertEqual(initial["status"], "NEUTRAL")
        self.assertIsNone(initial["updated_at"])
        self.assertEqual(get_company_preference(self.store, "other", self.job["company_id"])["status"], "NEUTRAL")

        saved = set_company_preference(
            self.store, "fixture", self.job["company_id"], "BLACKLISTED", "request-blacklist")
        self.assertEqual(saved["status"], "BLACKLISTED")
        self.assertTrue(saved["updated_at"])
        current = get_company_preference(self.store, "fixture", self.job["company_id"])
        self.assertEqual({key: current[key] for key in current},
                         {key: saved[key] for key in current})
        self.assertEqual(get_company_preference(self.store, "other", self.job["company_id"])["status"], "NEUTRAL")
        reopened = Store(self.store.url)
        reopened.migrate()
        try:
            after_reopen = get_company_preference(reopened, "fixture", self.job["company_id"])
            self.assertEqual(after_reopen["status"], "BLACKLISTED")
            self.assertEqual(after_reopen["updated_at"], saved["updated_at"])
        finally:
            reopened.engine.dispose()

    def test_preferred_and_neutral_changes_replace_the_current_projection(self):
        preferred = set_company_preference(
            self.store, "fixture", self.job["company_id"], "PREFERRED", "request-preferred")
        neutral = set_company_preference(
            self.store, "fixture", self.job["company_id"], "NEUTRAL", "request-neutral")
        self.assertEqual(preferred["status"], "PREFERRED")
        self.assertEqual(neutral["status"], "NEUTRAL")
        current = get_company_preference(self.store, "fixture", self.job["company_id"])
        self.assertEqual({key: current[key] for key in current},
                         {key: neutral[key] for key in current})
        listed = list_company_preferences(self.store, "fixture")
        self.assertEqual([item["company_id"] for item in listed], [self.job["company_id"]])
        self.assertEqual(listed[0]["status"], "NEUTRAL")
        self.assertEqual(listed[0]["company"], "Northwind Systems")

    def test_set_request_replay_is_idempotent_and_payload_reuse_conflicts(self):
        first = set_company_preference(
            self.store, "fixture", self.job["company_id"], "BLACKLISTED", "stable-request")
        replay = set_company_preference(
            self.store, "fixture", self.job["company_id"], "BLACKLISTED", "stable-request")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            {key: value for key, value in replay.items() if key != "replayed"},
            {key: value for key, value in first.items() if key != "replayed"},
        )
        with self.assertRaises(CompanyPreferenceConflict):
            set_company_preference(
                self.store, "fixture", self.job["company_id"], "PREFERRED", "stable-request")
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == "fixture", m.audit.c.action == "COMPANY_PREFERENCE")))
        self.assertEqual(len(events), 1)

    def test_explicit_company_preference_is_used_by_advisory_priority(self):
        recalculate(self.store, "fixture")
        job = self.store.jobs(profile_id="fixture")[0]
        set_company_preference(
            self.store, "fixture", job["company_id"], "PREFERRED", "priority-preferred")
        preferred = rank_jobs_by_application_priority(
            self.store, "fixture", [dict(job)])[0]["application_priority_score"]
        set_company_preference(
            self.store, "fixture", job["company_id"], "NEUTRAL", "priority-neutral")
        neutral = rank_jobs_by_application_priority(
            self.store, "fixture", [dict(job)])[0]["application_priority_score"]
        self.assertGreater(preferred, neutral)

    def test_unknown_profile_company_and_status_are_rejected(self):
        with self.assertRaises(LookupError):
            get_company_preference(self.store, "missing", self.job["company_id"])
        with self.assertRaises(LookupError):
            get_company_preference(self.store, "fixture", "missing-company")
        with self.assertRaises(ValueError):
            set_company_preference(self.store, "fixture", self.job["company_id"], "BLOCKED", "bad-status")
