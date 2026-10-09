"""Append-only, advisory feedback for a profile's matched jobs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from services.matching.user_match_feedback import (
    FEEDBACK_CATEGORIES,
    FeedbackConflict,
    apply_feedback_preference,
    list_feedback_preferences,
    record_feedback,
)
from tests.integration.test_spine import PROFILE


class UserMatchFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'feedback.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.job = capture_manual_job(self.store, {
            "request_id": "feedback-job-1",
            "title": "Backend Engineer",
            "company": "Example One",
            "description": "Build Python services with AWS.",
            "reference_url": "https://jobs.example.invalid/feedback-1",
            "application_url": "https://apply.example.invalid/feedback-1",
            "profile_id": "fixture",
        })["job"]
        recalculate(self.store, "fixture")

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_every_supported_category_is_profile_and_job_scoped_advisory_data(self):
        for index, category in enumerate(sorted(FEEDBACK_CATEGORIES)):
            result = record_feedback(
                self.store, "fixture", self.job["id"], category, f"request-{index}")
            self.assertEqual(result["profile_id"], "fixture")
            self.assertEqual(result["job_id"], self.job["id"])
            self.assertEqual(result["feedback"]["category"], category)
            self.assertTrue(result["preference_proposal"]["requires_confirmation"])
            self.assertTrue(result["preference_proposal"]["advisory"])

        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == "fixture",
                m.audit.c.action == "MATCH_FEEDBACK")).mappings().all()
        self.assertEqual(len(rows), len(FEEDBACK_CATEGORIES))
        self.assertTrue(all(row["payload"]["job_id"] == self.job["id"] for row in rows))

    def test_user_must_apply_a_matching_saved_proposal_and_replay_is_idempotent(self):
        feedback = record_feedback(
            self.store, "fixture", self.job["id"], "BAD_SALARY", "feedback-to-apply")
        preference = apply_feedback_preference(
            self.store, "fixture", feedback["feedback"]["id"],
            "salary", "decrease", "preference-apply-1")
        replay = apply_feedback_preference(
            self.store, "fixture", feedback["feedback"]["id"],
            "salary", "decrease", "preference-apply-1")

        self.assertFalse(preference["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(preference, {**replay, "replayed": False})
        stored = list_feedback_preferences(self.store, "fixture")["items"]
        self.assertEqual(len(stored), 1)
        for key in ("id", "job_id", "company_id", "feedback_id", "dimension", "direction", "created_at"):
            self.assertEqual(stored[0][key], preference["preference"][key])

    def test_preference_rejects_review_only_direction_and_request_id_reuse_conflicts(self):
        feedback = record_feedback(
            self.store, "fixture", self.job["id"], "BAD_SALARY", "feedback-scope")
        feedback_id = feedback["feedback"]["id"]

        with self.assertRaises(ValueError):
            apply_feedback_preference(
                self.store, "fixture", feedback_id, "salary", "review", "preference-scope")
        with self.assertRaises(LookupError):
            apply_feedback_preference(
                self.store, "other-profile", feedback_id, "salary", "decrease", "other-scope")

        applied = apply_feedback_preference(
            self.store, "fixture", feedback_id, "salary", "decrease", "preference-reuse")
        with self.assertRaises(FeedbackConflict):
            apply_feedback_preference(
                self.store, "fixture", feedback_id, "salary", "increase", "preference-reuse")
        self.assertEqual(applied["preference"]["direction"], "DECREASE")

    def test_preferences_are_profile_scoped_and_latest_decision_is_read_back(self):
        one = record_feedback(
            self.store, "fixture", self.job["id"], "BAD_SALARY", "feedback-one")
        two = record_feedback(
            self.store, "fixture", self.job["id"], "GOOD_MATCH", "feedback-two")
        apply_feedback_preference(
            self.store, "fixture", one["feedback"]["id"], "salary", "decrease", "apply-one")
        apply_feedback_preference(
            self.store, "fixture", two["feedback"]["id"], "salary", "increase", "apply-two")

        current = list_feedback_preferences(self.store, "fixture")
        self.assertEqual(len(current["items"]), 2)
        self.assertEqual(current["by_dimension"]["salary"]["direction"], "INCREASE")
        self.assertNotIn("request_id", current["items"][0])

    def test_reviewed_seniority_feedback_keeps_only_a_known_level_snapshot(self):
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.jobs.c.attributes).where(
                m.jobs.c.id == self.job["id"])).mappings().one()
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"])
                               .values(attributes={**row["attributes"], "experience_level": "SENIOR"}))

        feedback = record_feedback(
            self.store, "fixture", self.job["id"], "TOO_SENIOR", "feedback-seniority")
        applied = apply_feedback_preference(
            self.store, "fixture", feedback["feedback"]["id"],
            "seniority", "increase", "apply-seniority")
        current = list_feedback_preferences(self.store, "fixture")["items"]

        self.assertEqual(applied["preference"]["seniority_level"], "SENIOR")
        self.assertEqual(current[0]["seniority_level"], "SENIOR")

        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.jobs.c.attributes).where(
                m.jobs.c.id == self.job["id"])).mappings().one()
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"])
                               .values(attributes={**row["attributes"], "experience_level": "UNVERIFIED"}))
        unknown_feedback = record_feedback(
            self.store, "fixture", self.job["id"], "TOO_SENIOR", "feedback-seniority-unknown")
        unknown = apply_feedback_preference(
            self.store, "fixture", unknown_feedback["feedback"]["id"],
            "seniority", "increase", "apply-seniority-unknown")
        self.assertIsNone(unknown["preference"]["seniority_level"])

    def test_replaying_a_pre_upgrade_preference_does_not_reclassify_its_source_job(self):
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.jobs.c.attributes).where(
                m.jobs.c.id == self.job["id"])).mappings().one()
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"])
                               .values(attributes={**row["attributes"], "experience_level": "SENIOR"}))
        feedback = record_feedback(
            self.store, "fixture", self.job["id"], "TOO_SENIOR", "legacy-seniority-feedback")
        first = apply_feedback_preference(
            self.store, "fixture", feedback["feedback"]["id"],
            "seniority", "increase", "legacy-seniority-apply")
        with self.store.engine.begin() as connection:
            applied_event = connection.execute(select(m.audit.c.id, m.audit.c.payload).where(
                m.audit.c.id == first["preference"]["id"])).mappings().one()
            legacy_payload = dict(applied_event["payload"])
            legacy_payload.pop("seniority_level")
            connection.execute(update(m.audit).where(m.audit.c.id == applied_event["id"])
                               .values(payload=legacy_payload))

        replay = apply_feedback_preference(
            self.store, "fixture", feedback["feedback"]["id"],
            "seniority", "increase", "legacy-seniority-apply")

        self.assertTrue(replay["replayed"])
        self.assertIsNone(replay["preference"]["seniority_level"])
        self.assertIsNone(list_feedback_preferences(self.store, "fixture")["items"][0]["seniority_level"])

    def test_same_request_and_category_replays_one_event(self):
        first = record_feedback(
            self.store, "fixture", self.job["id"], "GOOD_MATCH", "request-replay")
        replay = record_feedback(
            self.store, "fixture", self.job["id"], "GOOD_MATCH", "request-replay")

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first, {**replay, "replayed": False})
        with self.store.engine.connect() as connection:
            feedback_count = connection.execute(select(m.audit.c.id).where(
                m.audit.c.action == "MATCH_FEEDBACK")).all()
        self.assertEqual(len(feedback_count), 1)

    def test_replay_detection_does_not_depend_on_timestamp_precision(self):
        with patch("services.matching.user_match_feedback.now", return_value="same-time"):
            first = record_feedback(
                self.store, "fixture", self.job["id"], "GOOD_MATCH", "fixed-clock")
            replay = record_feedback(
                self.store, "fixture", self.job["id"], "GOOD_MATCH", "fixed-clock")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])

    def test_reusing_request_for_another_category_or_job_conflicts(self):
        record_feedback(self.store, "fixture", self.job["id"], "GOOD_MATCH", "request-fixed")
        with self.assertRaises(FeedbackConflict):
            record_feedback(self.store, "fixture", self.job["id"], "BAD_MATCH", "request-fixed")

        other_job = capture_manual_job(self.store, {
            "request_id": "feedback-job-2",
            "title": "Data Engineer",
            "company": "Example Two",
            "description": "Build data pipelines.",
            "reference_url": "https://jobs.example.invalid/feedback-2",
            "profile_id": "fixture",
        })["job"]
        recalculate(self.store, "fixture")
        with self.assertRaises(FeedbackConflict):
            record_feedback(self.store, "fixture", other_job["id"], "GOOD_MATCH", "request-fixed")

    def test_missing_or_cross_profile_match_is_rejected_without_writes(self):
        other = deepcopy(PROFILE)
        other["id"] = "other-profile"
        other["name"] = "Other"
        self.store.save_profile(other)
        before = self.store.count("audit_events")

        with self.assertRaises(LookupError):
            record_feedback(self.store, "other-profile", self.job["id"], "GOOD_MATCH", "foreign")
        with self.assertRaises(LookupError):
            record_feedback(self.store, "fixture", "missing-job", "GOOD_MATCH", "missing")

        self.assertEqual(self.store.count("audit_events"), before)

    def test_invalid_category_or_request_id_is_rejected_without_writes(self):
        before = self.store.count("audit_events")
        for category, request_id in (("AUTO_APPLY", "valid-request"), ("GOOD_MATCH", "")):
            with self.subTest(category=category, request_id=request_id):
                with self.assertRaises(ValueError):
                    record_feedback(self.store, "fixture", self.job["id"], category, request_id)
        self.assertEqual(self.store.count("audit_events"), before)

    def test_feedback_never_changes_preferences_or_hard_rules(self):
        profile_before = self.store.profile("fixture")
        result = record_feedback(
            self.store, "fixture", self.job["id"], "WRONG_LOCATION", "proposal-only")
        profile_after = self.store.profile("fixture")

        self.assertEqual(profile_after["preferences"], profile_before["preferences"])
        self.assertEqual(result["preference_proposal"]["dimension"], "location")
        self.assertEqual(result["preference_proposal"]["direction"], "INCREASE")
        self.assertTrue(result["preference_proposal"]["requires_confirmation"])
        self.assertFalse(any(key in result["preference_proposal"] for key in (
            "eligibility", "hard_rule", "application_action", "auto_submit")))


if __name__ == "__main__":
    unittest.main()
