"""Bound answer-review resume admission and exact worker-result readback."""
from copy import deepcopy
import json
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
from services.applications.answer_resume import _binding
from services.applications.preflight import latest_preflight, persist_preflight
from services.applications.run_requests import queue_mock_run
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

    def test_reprepared_documents_reject_cached_answer_review_before_admission(self):
        cached_payload = self.payload("stale-document-review")
        before = self.store.application(self.application_id)
        before_preflight = latest_preflight(self.store, self.application_id)
        self.assertTrue(before_preflight["is_current"])

        # Reprepare through the real document service, without changing the
        # reviewed profile, answer, question, or form metadata fingerprint.
        prepared_id = prepare(
            self.store, before["job_id"], "fixture", self.root,
            cover_letter_style="technical",
        )
        self.assertEqual(prepared_id, self.application_id)
        current = self.store.application(self.application_id)
        self.assertEqual(current["profile_version"], before["profile_version"])
        self.assertEqual(current["route_id"], before["route_id"])
        self.assertEqual(current["document_version"], before["document_version"] + 1)
        stale_preflight = latest_preflight(self.store, self.application_id)
        self.assertEqual(stale_preflight["event_id"], before_preflight["event_id"])
        self.assertEqual(stale_preflight["metadata_fingerprint"], FORM_FINGERPRINT)
        self.assertFalse(stale_preflight["is_current"])

        refused = self.client.post(self.base, json=cached_payload)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(
            refused.json()["detail"]["code"], "APPLICATION_ANSWER_REVIEW_STALE",
        )
        self.assertEqual(self.apply_mock_operation_count(), 0)
        with self.store.engine.connect() as connection:
            requested_events = connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_RUN_REQUESTED_V1",
            )).scalar_one()
        self.assertEqual(requested_events, 0)

        # New evidence for the current document revision makes the same
        # approved answer eligible again; the historical record is retained.
        persist_preflight(self.store, self.application_id, {
            **before_preflight,
            "document_version": current["document_version"],
            "observed_at": "2026-10-29T00:01:00Z",
        })
        refreshed = self.payload("current-document-review")
        self.assertEqual(refreshed["answer_id"], cached_payload["answer_id"])
        self.assertEqual(refreshed["form_fingerprint"], cached_payload["form_fingerprint"])
        admitted = self.client.post(self.base, json=refreshed)
        self.assertEqual(admitted.status_code, 202, admitted.text)
        self.assertEqual(self.apply_mock_operation_count(), 1)

    def test_legacy_queued_resume_rejects_stale_preflight_before_adapter_access(self):
        payload = self.payload("legacy-stale-document-review")
        historical_binding = _binding(
            self.store, "fixture", self.application_id, payload,
        )
        before = self.store.application(self.application_id)
        prepare(
            self.store, before["job_id"], "fixture", self.root,
            cover_letter_style="technical",
        )
        current = self.store.application(self.application_id)
        self.assertEqual(current["document_version"], before["document_version"] + 1)
        self.assertEqual(current["profile_version"], before["profile_version"])
        stale_preflight = latest_preflight(self.store, self.application_id)
        self.assertFalse(stale_preflight["is_current"])
        self.assertEqual(
            stale_preflight["event_id"], historical_binding["preflight_event_id"],
        )

        # Model an operation admitted by the old code: its document snapshot
        # is current but its reviewed-answer binding still names the old,
        # unchanged preflight event. No newer event ID masks this defect.
        queued = queue_mock_run(
            self.store, "fixture", self.application_id, submit=False,
            request_id=payload["request_id"], resume_binding=historical_binding,
        )
        self.assertEqual(queued["state"], "QUEUED")
        result, mocked = self.execute(
            queued["operation_id"],
            lambda *_args, **_kwargs: self.fail("adapter must not start"),
        )
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(
            self.operation_error(queued["operation_id"]),
            "APPLICATION_ANSWER_REVIEW_STALE",
        )
        mocked.assert_not_called()
        self.assertEqual(self.store.application(self.application_id), current)

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
        self.assertEqual(original["answer"]["text"], ANSWER["text"])

        generic_list = self.client.get("/operations")
        self.assertEqual(generic_list.status_code, 200, generic_list.text)
        generic_detail = self.client.get(
            "/operations/" + admitted.json()["operation_id"],
        )
        self.assertEqual(generic_detail.status_code, 200, generic_detail.text)
        for generic in (generic_list.json(), generic_detail.json()):
            with self.subTest(endpoint="generic-operation"):
                self.assertNotIn(ANSWER["text"], json.dumps(generic))
                self.assertNotIn("resume_result", json.dumps(generic))

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
