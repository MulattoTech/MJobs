"""Grounded application follow-up draft candidates."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from services.tracking.follow_up_drafts import draft_follow_up_candidates


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self, applications_by_profile):
        self.applications_by_profile = applications_by_profile

    def profile(self, profile_id):
        if profile_id not in self.applications_by_profile:
            raise LookupError(profile_id)
        return {"id": profile_id}

    def applications(self, profile_id=None):
        return list(self.applications_by_profile.get(profile_id, []))


def application(application_id="app-1", profile_id="p1", *, title="Network Admin",
                company="Example Co", created_at="2026-10-01T00:00:00Z",
                state="PREPARED"):
    return {"id": application_id, "profile_id": profile_id, "job_id": "job-1",
            "title": title, "company": company, "created_at": created_at,
            "state": state}


def status(status="APPLIED", attribution="USER_REPORTED", at=None, sequence=1):
    return {"type": "status", "status": status, "attribution": attribution,
            "timestamp": (at or NOW - timedelta(days=10)).isoformat(),
            "sequence": sequence}


class FollowUpDraftTests(unittest.TestCase):
    def setUp(self):
        self.store = Store({"p1": [application()], "p2": [application("app-2", "p2")]})
        self.history = {"app-1": {"history": [status()]},
                        "app-2": {"history": [status()]}}

    def run_drafts(self, profile_id="p1", quiet_days=7, now=NOW):
        with patch("services.tracking.follow_up_drafts.list_application_tracking",
                   side_effect=lambda store, profile, ids: {
                       app_id: self.history.get(app_id, {"history": []})
                       for app_id in ids}):
            return draft_follow_up_candidates(self.store, profile_id, quiet_days, now=now)

    def test_includes_user_reported_applied_status_and_explicit_not_verified_drafts(self):
        result = self.run_drafts()
        self.assertEqual(result["quiet_days"], 7)
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(set(candidate), {
            "application_id", "job_id", "job_title", "company", "days_quiet",
            "reported_at", "attribution", "verified", "email_subject",
            "email_body", "dm_body", "character_limits"})
        self.assertEqual(candidate["application_id"], "app-1")
        self.assertEqual(candidate["job_id"], "job-1")
        self.assertEqual(candidate["reported_at"], (NOW - timedelta(days=10)).isoformat())
        self.assertEqual(candidate["attribution"], "USER_REPORTED")
        self.assertIs(candidate["verified"], False)
        self.assertEqual(candidate["character_limits"], {
            "subject": 120, "email_body": 500, "dm_body": 280})
        self.assertTrue(candidate["email_subject"])
        self.assertTrue(candidate["email_body"])
        self.assertTrue(candidate["dm_body"])

    def test_accepts_external_user_report_and_excludes_unattributed_status(self):
        self.history["app-1"] = {"history": [status(attribution="EXTERNAL_USER_REPORTED")]}
        self.assertEqual(self.run_drafts()["candidates"][0]["attribution"],
                         "EXTERNAL_USER_REPORTED")
        self.history["app-1"] = {"history": [status(attribution="AUTOMATION") ]}
        self.assertEqual(self.run_drafts()["candidates"], [])

    def test_quiet_period_boundary_is_inclusive_and_future_status_is_excluded(self):
        self.history["app-1"] = {"history": [status(at=NOW - timedelta(days=7))]}
        self.assertEqual(len(self.run_drafts(quiet_days=7)["candidates"]), 1)
        self.history["app-1"] = {"history": [status(at=NOW + timedelta(seconds=1))]}
        self.assertEqual(self.run_drafts(quiet_days=1)["candidates"], [])

    def test_latest_pipeline_status_controls_eligibility_and_notes_do_not(self):
        old = status(at=NOW - timedelta(days=30), sequence=1)
        self.history["app-1"] = {"history": [old,
            {"type": "note", "sequence": 2, "timestamp": NOW.isoformat()},
            status("INTERVIEW", sequence=3, at=NOW - timedelta(days=9))]}
        self.assertEqual(self.run_drafts()["candidates"], [])

    def test_automation_lifecycle_state_without_user_reported_status_does_not_qualify(self):
        self.store.applications_by_profile["p1"] = [application(state="EXTERNALLY_APPLIED")]
        self.history["app-1"] = {"history": []}
        self.assertEqual(self.run_drafts()["candidates"], [])

    def test_profiles_are_isolated(self):
        with patch("services.tracking.follow_up_drafts.list_application_tracking",
                   return_value={"app-2": {"history": [status()]}}) as tracking:
            result = draft_follow_up_candidates(self.store, "p2", 7, now=NOW)
        self.assertEqual([row["application_id"] for row in result["candidates"]], ["app-2"])
        tracking.assert_called_once_with(self.store, "p2", ["app-2"])

    def test_rejects_bad_quiet_period_and_clock_values(self):
        for quiet_days in (0, 91, True, 1.5, "7", None):
            with self.subTest(quiet_days=quiet_days), self.assertRaises(ValueError):
                self.run_drafts(quiet_days=quiet_days)
        for now in ("2026-10-08T12:00:00Z", datetime(2026, 10, 8)):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.run_drafts(now=now)

    def test_scrubs_contacts_from_dynamic_job_metadata_and_caps_all_drafts(self):
        self.store.applications_by_profile["p1"] = [application(
            title="Architect " + "x" * 160 + " alice@example.com https://secret.example/app @recruiter 555-555-1212",
            company="C" * 220)]
        result = self.run_drafts()
        candidate = result["candidates"][0]
        self.assertLessEqual(len(candidate["email_subject"]), 120)
        self.assertLessEqual(len(candidate["email_body"]), 500)
        self.assertLessEqual(len(candidate["dm_body"]), 280)
        for field in ("job_title", "company", "email_subject", "email_body", "dm_body"):
            value = candidate[field]
            self.assertNotIn("alice@example.com", value)
            self.assertNotIn("secret.example", value)
            self.assertNotIn("@recruiter", value)
            self.assertNotIn("555-555-1212", value)


if __name__ == "__main__":
    unittest.main()
