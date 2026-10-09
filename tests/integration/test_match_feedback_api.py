"""Profile/job-scoped match feedback persistence contract."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.match_feedback import create_match_feedback_router
from packages.persistence import models as m
from packages.persistence.store import Store


PROFILE = {
    "id": "fixture", "name": "Synthetic User", "email": "user@example.test",
    "facts": [], "answers": [],
    "preferences": {
        "work_arrangements": ["REMOTE"],
        "salary_expectations": "$65,000+",
        "avoid_terms": ["unpaid"],
    },
}
FEEDBACK_CATEGORIES = (
    "GOOD_MATCH", "BAD_MATCH", "TOO_SENIOR", "WRONG_TECHNOLOGY",
    "WRONG_LOCATION", "BAD_SALARY", "BAD_COMPANY", "NOT_INTERESTED",
)


class MatchFeedbackApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-match-feedback-api-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'feedback.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        self._seed_job_and_match()

        app = FastAPI()
        app.include_router(create_match_feedback_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_job_and_match(self):
        profile = self.store.profile("fixture")
        other = self.store.profile("other")
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Synthetic Employer", domain="employer.example",
            ))
            connection.execute(m.jobs.insert().values(
                id="job", company_id="company", requisition="req-job",
                title="Systems Administrator", description="Synthetic description",
                skills=["linux", "networking"], location="Remote", remote_type="REMOTE",
                attributes={"hard_eligibility": {"remote_only": True}},
            ))
            connection.execute(m.jobs.insert().values(
                id="other-job", company_id="company", requisition="req-other-job",
                title="Network Administrator", description="Another synthetic description",
                skills=["networking"], location="Remote", remote_type="REMOTE",
                attributes={"hard_eligibility": {"remote_only": True}},
            ))
            connection.execute(m.matches.insert().values(
                id="fixture-job-match", job_id="job", profile_id="fixture",
                profile_version=profile["version"], score=81.5, missing=[],
                components={"hard_eligibility": {"eligible": True}},
            ))
            connection.execute(m.matches.insert().values(
                id="other-job-match", job_id="other-job", profile_id="other",
                profile_version=other["version"], score=61.0, missing=[],
                components={"hard_eligibility": {"eligible": True}},
            ))

    def _url(self, profile_id="fixture", job_id="job"):
        return f"/profiles/{profile_id}/jobs/{job_id}/match-feedback"

    def _post(self, *, category="GOOD_MATCH", request_id="request-1",
              profile_id="fixture", job_id="job"):
        return self.client.post(self._url(profile_id, job_id), json={
            "category": category,
            "request_id": request_id,
        })

    def _table_rows(self, table):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id),
            ).mappings()]

    def _hard_rule_snapshot(self):
        return {
            "profile_version": self.store.profile("fixture")["version"],
            "preferences": self.store.profile("fixture")["preferences"],
            "matches": self._table_rows(m.matches),
            "applications": self._table_rows(m.applications),
            "operations": self._table_rows(m.operations),
        }

    def test_feedback_is_persisted_scoped_and_replayed_by_request_id(self):
        before = self._hard_rule_snapshot()

        first = self._post()
        retry = self._post()

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(retry.status_code, 200, retry.text)
        created = first.json()
        replay = retry.json()
        self.assertEqual(set(created), {
            "profile_id", "job_id", "feedback", "replayed", "preference_proposal",
        })
        self.assertEqual(created["profile_id"], "fixture")
        self.assertEqual(created["job_id"], "job")
        self.assertFalse(created["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(created["feedback"]["category"], "GOOD_MATCH")
        self.assertTrue(created["feedback"]["id"])
        self.assertTrue(created["feedback"]["created_at"])
        self.assertEqual(replay["feedback"], created["feedback"])
        self.assertEqual(replay["preference_proposal"], created["preference_proposal"])
        proposal = created["preference_proposal"]
        if proposal is not None:
            self.assertEqual(set(proposal), {
                "dimension", "direction", "explanation", "review_required",
            })
            self.assertTrue(proposal["dimension"])
            self.assertTrue(proposal["direction"])
            self.assertTrue(proposal["explanation"])
            self.assertIs(proposal["review_required"], True)
        self.assertEqual(self._hard_rule_snapshot(), before)

    def test_all_feedback_categories_are_accepted_without_hard_rule_changes(self):
        before = self._hard_rule_snapshot()

        for index, category in enumerate(FEEDBACK_CATEGORIES):
            with self.subTest(category=category):
                response = self._post(category=category, request_id=f"category-{index}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["feedback"]["category"], category)
                self.assertIn("replayed", response.json())

        self.assertEqual(self._hard_rule_snapshot(), before)

    def test_preference_proposals_are_deterministic_and_history_is_read_only(self):
        first = self._post(category="WRONG_LOCATION", request_id="location-feedback-1")
        second = self._post(category="WRONG_LOCATION", request_id="location-feedback-2")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIsNotNone(first.json()["preference_proposal"])
        self.assertEqual(
            first.json()["preference_proposal"],
            second.json()["preference_proposal"],
        )
        self.assertIs(first.json()["preference_proposal"]["review_required"], True)

        before_read = self._table_rows(m.audit)
        history = self.client.get(self._url())

        self.assertEqual(history.status_code, 200, history.text)
        body = history.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["job_id"], "job")
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual({item["category"] for item in body["items"]}, {"WRONG_LOCATION"})
        self.assertIn(body["latest"], body["items"])
        self.assertEqual(self._table_rows(m.audit), before_read)

    def test_reusing_request_id_for_another_category_conflicts_without_mutating_rules(self):
        first = self._post(category="BAD_MATCH", request_id="same-request")
        self.assertEqual(first.status_code, 200, first.text)
        before_conflict = self._hard_rule_snapshot()

        conflict = self._post(category="GOOD_MATCH", request_id="same-request")

        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("code", conflict.json().get("detail", {}))
        self.assertTrue(conflict.json()["detail"]["code"].startswith("MATCH_FEEDBACK_"))
        self.assertNotIn("same-request", conflict.text)
        self.assertEqual(self._hard_rule_snapshot(), before_conflict)

    def test_missing_and_cross_profile_objects_return_sanitized_not_found(self):
        before = self._hard_rule_snapshot()
        responses = [
            self._post(profile_id="missing-profile", request_id="private-request"),
            self._post(profile_id="other", request_id="private-request"),
            self._post(job_id="missing-job", request_id="private-request"),
        ]

        for response in responses:
            with self.subTest(status=response.status_code, body=response.text):
                self.assertEqual(response.status_code, 404)
                self.assertIn("code", response.json().get("detail", {}))
                self.assertTrue(response.json()["detail"]["code"].startswith("MATCH_FEEDBACK_"))
                self.assertNotIn("private-request", response.text)
        self.assertEqual(self._hard_rule_snapshot(), before)

    def test_invalid_payload_never_creates_feedback_or_changes_hard_rules(self):
        before = self._hard_rule_snapshot()
        invalid_payloads = [
            {},
            {"category": "INFERRED", "request_id": "invalid-1"},
            {"category": "GOOD_MATCH", "request_id": "  "},
            {"category": "GOOD_MATCH", "request_id": "invalid-2", "extra": True},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post(self._url(), json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("invalid-", response.text)
        self.assertEqual(self._hard_rule_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
