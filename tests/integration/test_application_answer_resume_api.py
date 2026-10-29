"""Bound answer-review resume admission and exact worker-result readback."""
from copy import deepcopy
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.preflight import persist_preflight
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


QUESTION_ID = "manual-azure-review"
QUESTION = "Describe your experience managing Azure subscriptions."
ANSWER = {
    "id": "approved-azure-answer",
    "question": QUESTION,
    "text": "I managed synthetic Azure subscriptions.",
    "source": "User reviewed application answer",
    "confidence": 100,
    "verified": True,
    "user_approved": True,
}
FORM_FINGERPRINT = "a" * 64


class ApplicationAnswerResumeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-answer-resume-api-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'resume.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = deepcopy(PROFILE)
        profile["answers"] = [*profile.get("answers", []), ANSWER]
        self.store.save_profile(profile)
        other = deepcopy(PROFILE)
        other.update(id="other-profile", email="other@example.invalid", answers=[])
        self.store.save_profile(other)
        self.fixture_context = fixture_server()
        self.origin = self.fixture_context.__enter__()
        discover(self.store, self.origin, "fixture")
        job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(self.application_id)
        persist_preflight(self.store, self.application_id, {
            "result": "FAIL",
            "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
            "observed_at": "2026-10-29T00:00:00Z",
            "profile_version": application["profile_version"],
            "document_version": application["document_version"],
            "route_id": application["route_id"],
            "provider": "mock-ats",
            "tenant": "example",
            "requisition": "B",
            "metadata_fingerprint": FORM_FINGERPRINT,
            "mappings": [],
            "missing": [{"question_id": QUESTION_ID, "question": QUESTION}],
        })
        self.client = TestClient(create_app(
            self.store, self.origin, self.root, operation_mode="FIXTURE",
        ))
        self.base = (
            f"/profiles/fixture/applications/{self.application_id}"
            f"/answer-review-resumes"
        )

    def tearDown(self):
        self.client.close()
        self.fixture_context.__exit__(None, None, None)
        self.store.engine.dispose()
        self.temp.cleanup()

    def review(self):
        response = self.client.get(self.base + f"/questions/{QUESTION_ID}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def payload(self, request_id="resume-1", **changes):
        review = self.review()
        return {
            "request_id": request_id,
            "question_id": QUESTION_ID,
            "question_fingerprint": review["question"]["fingerprint"],
            "form_fingerprint": review["form_fingerprint"],
            "answer_id": ANSWER["id"],
            "expected_profile_version": review["profile_version"],
            **changes,
        }

    def apply_mock_operation_count(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.operations).where(
                m.operations.c.kind == "APPLY_MOCK",
            )).scalar_one()

    def execute(self, operation_id, apply_mock):
        with patch.dict(os.environ, {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_MOCK_ORIGIN": self.origin,
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }), patch("workers.tasks.apply_mock", side_effect=apply_mock) as mocked:
            result = execute_operation(operation_id)
        return result, mocked

    def operation_error(self, operation_id):
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.operations.c.error).where(
                m.operations.c.id == operation_id,
            )).scalar_one()

    def complete_with_matching_preflight(self, _store, application_id, *_args, **_kwargs):
        application = self.store.application(application_id)
        persist_preflight(self.store, application_id, {
            "result": "PASS", "reason": None,
            "observed_at": "2026-10-29T00:03:00Z",
            "profile_version": application["profile_version"],
            "document_version": application["document_version"],
            "route_id": application["route_id"],
            "metadata_fingerprint": FORM_FINGERPRINT,
            "mappings": [{"question_id": QUESTION_ID, "question": QUESTION}],
            "missing": [],
        })
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == application_id,
            ).values(state="DRY_RUN_COMPLETE"))
        return self.store.application(application_id)

    def test_scoped_review_admission_and_completed_exact_readback(self):
        review = self.review()
        self.assertEqual(review["profile_id"], "fixture")
        self.assertEqual(review["application_id"], self.application_id)
        self.assertEqual(review["form_fingerprint"], FORM_FINGERPRINT)
        self.assertEqual(review["question"]["question_id"], QUESTION_ID)
        self.assertRegex(review["question"]["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(review["answer_bank"][-1]["id"], ANSWER["id"])

        admitted = self.client.post(self.base, json=self.payload())
        self.assertEqual(admitted.status_code, 202, admitted.text)
        receipt = admitted.json()
        self.assertEqual(receipt["state"], "QUEUED")
        self.assertFalse(receipt["external_submission_enabled"])
        pending = self.client.get(self.base + "/resume-1")
        self.assertEqual(pending.status_code, 202, pending.text)
        self.assertEqual(pending.json()["operation_id"], receipt["operation_id"])

        result, mocked = self.execute(
            receipt["operation_id"], self.complete_with_matching_preflight,
        )
        self.assertEqual(result["status"], "COMPLETED", result)
        mocked.assert_called_once()

        completed = self.client.get(self.base + "/resume-1")
        self.assertEqual(completed.status_code, 200, completed.text)
        value = completed.json()
        self.assertEqual(value["request_id"], "resume-1")
        self.assertEqual(value["profile_id"], "fixture")
        self.assertEqual(value["application_id"], self.application_id)
        self.assertEqual(value["question_id"], QUESTION_ID)
        self.assertEqual(value["question_fingerprint"], review["question"]["fingerprint"])
        self.assertEqual(value["form_fingerprint"], FORM_FINGERPRINT)
        self.assertEqual(value["profile_version"], review["profile_version"])
        self.assertEqual(value["external_submission"], "OFF")
        self.assertEqual(value["answer"]["id"], ANSWER["id"])
        self.assertTrue(value["answer"]["user_approved"])
        self.assertEqual(value["preflight"], {"result": "PASS", "is_current": True})

    def test_stale_unknown_cross_scope_and_unapproved_inputs_never_admit(self):
        valid = self.payload()
        changes = [
            {"question_fingerprint": "b" * 64},
            {"form_fingerprint": "b" * 64},
            {"answer_id": "other-answer"},
            {"expected_profile_version": "b" * 64},
        ]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                response = self.client.post(self.base, json={
                    **valid, "request_id": f"rejected-{index}", **change,
                })
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.apply_mock_operation_count(), 0)

        foreign = self.client.post(self.base.replace("/fixture/", "/other-profile/"), json=valid)
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(self.apply_mock_operation_count(), 0)

        other_job = next(row for row in self.store.jobs("fixture")
                         if row["requisition"] != "B")
        other_application_id = prepare(
            self.store, other_job["id"], "fixture", self.root,
        )
        other_application = self.store.application(other_application_id)
        persist_preflight(self.store, other_application_id, {
            "result": "FAIL", "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
            "observed_at": "2026-10-29T00:00:30Z",
            "profile_version": other_application["profile_version"],
            "document_version": other_application["document_version"],
            "route_id": other_application["route_id"],
            "metadata_fingerprint": FORM_FINGERPRINT,
            "mappings": [],
            "missing": [{"question_id": QUESTION_ID, "question": QUESTION}],
        })
        cross_application = self.client.post(
            f"/profiles/fixture/applications/{other_application_id}"
            "/answer-review-resumes",
            json={**valid, "request_id": "cross-application"},
        )
        self.assertEqual(cross_application.status_code, 409, cross_application.text)
        self.assertEqual(self.apply_mock_operation_count(), 0)

        profile = self.store.profile("fixture")
        profile["answers"][-1]["user_approved"] = False
        self.store.save_profile(profile)
        unapproved = self.client.post(self.base, json={**valid, "request_id": "unapproved"})
        self.assertEqual(unapproved.status_code, 409, unapproved.text)
        self.assertEqual(self.apply_mock_operation_count(), 0)

    def test_failed_or_stale_worker_preflight_has_no_success_readback(self):
        admitted = self.client.post(self.base, json=self.payload("resume-fail"))
        self.assertEqual(admitted.status_code, 202, admitted.text)
        operation_id = admitted.json()["operation_id"]
        application = self.store.application(self.application_id)
        persist_preflight(self.store, self.application_id, {
            "result": "UNKNOWN",
            "reason": "STALE_PREFLIGHT",
            "observed_at": "2026-10-29T00:01:00Z",
            "profile_version": application["profile_version"],
            "document_version": application["document_version"],
            "route_id": application["route_id"],
            "metadata_fingerprint": "b" * 64,
            "mappings": [{"question_id": QUESTION_ID, "question": QUESTION}],
            "missing": [],
        })
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id,
            ).values(state="BLOCKED", error="APPLICATION_RESUME_PREFLIGHT_STALE"))
        blocked = self.client.get(self.base + "/resume-fail")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(
            blocked.json()["detail"]["code"], "APPLICATION_RESUME_PREFLIGHT_STALE",
        )

    def test_worker_rejects_changed_answer_binding_before_adapter_access(self):
        admitted = self.client.post(self.base, json=self.payload("changed-answer"))
        self.assertEqual(admitted.status_code, 202, admitted.text)
        profile = self.store.profile("fixture")
        profile["answers"][-1]["text"] = "Changed after resume admission."
        self.store.save_profile(profile)

        operation_id = admitted.json()["operation_id"]
        result, mocked = self.execute(
            operation_id,
            lambda *_args, **_kwargs: self.fail("adapter must not start"),
        )
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(
            self.operation_error(operation_id), "APPLICATION_ANSWER_REVIEW_STALE",
        )
        mocked.assert_not_called()

    def test_worker_blocks_changed_fresh_form_after_adapter_returns(self):
        admitted = self.client.post(self.base, json=self.payload("changed-form"))
        self.assertEqual(admitted.status_code, 202, admitted.text)

        def changed_form(_store, application_id, *_args, **_kwargs):
            application = self.store.application(application_id)
            persist_preflight(self.store, application_id, {
                "result": "PASS", "reason": None,
                "observed_at": "2026-10-29T00:02:00Z",
                "profile_version": application["profile_version"],
                "document_version": application["document_version"],
                "route_id": application["route_id"],
                "metadata_fingerprint": "b" * 64,
                "mappings": [{"question_id": QUESTION_ID, "question": QUESTION}],
                "missing": [],
            })
            with self.store.engine.begin() as connection:
                connection.execute(update(m.applications).where(
                    m.applications.c.id == application_id,
                ).values(state="DRY_RUN_COMPLETE"))
            return self.store.application(application_id)

        operation_id = admitted.json()["operation_id"]
        result, mocked = self.execute(operation_id, changed_form)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(
            self.operation_error(operation_id), "APPLICATION_RESUME_PREFLIGHT_STALE",
        )
        mocked.assert_called_once()

    def test_completed_resume_replays_immutable_result_after_profile_changes(self):
        admitted = self.client.post(self.base, json=self.payload("immutable-replay"))
        self.assertEqual(admitted.status_code, 202, admitted.text)

        result, mocked = self.execute(
            admitted.json()["operation_id"], self.complete_with_matching_preflight,
        )
        self.assertEqual(result["status"], "COMPLETED", result)
        mocked.assert_called_once()

        completed = self.client.get(self.base + "/immutable-replay")
        self.assertEqual(completed.status_code, 200, completed.text)
        original = completed.json()

        profile = self.store.profile("fixture")
        profile["answers"][-1]["text"] = "Edited after the completed operation."
        self.store.save_profile(profile)

        replayed = self.client.get(self.base + "/immutable-replay")
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json(), original)

    def test_completed_operation_without_fenced_result_fails_closed(self):
        admitted = self.client.post(self.base, json=self.payload("missing-result"))
        self.assertEqual(admitted.status_code, 202, admitted.text)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == admitted.json()["operation_id"],
            ).values(state="COMPLETED", error=None))

        replayed = self.client.get(self.base + "/missing-result")
        self.assertEqual(replayed.status_code, 409, replayed.text)
        self.assertEqual(
            replayed.json()["detail"]["code"], "APPLICATION_RESUME_RECEIPT_MISSING",
        )


if __name__ == "__main__":
    unittest.main()
