"""MJ-REQ-292 criterion 44: unknown ATS questions stop at durable review."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class UncertainQuestionReviewCriterion44Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-criterion44-review-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'review.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))

        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.review_url = "https://jobs.ashbyhq.com/fixture/engineering-123"
        with self.store.engine.begin() as connection:
            application = self.store.application(self.application_id)
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(
                provider="ashby", tenant="fixture", requisition="engineering-123",
                url=self.review_url,
            ))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
        self.client = self._client(self.store)

    def _client(self, store):
        return TestClient(create_app(
            store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _count(self, table, **filters):
        with self.store.engine.connect() as connection:
            query = select(table.c.id)
            for name, value in filters.items():
                query = query.where(getattr(table.c, name) == value)
            return len(connection.execute(query).all())

    def test_unknown_question_stops_at_reopened_durable_review_without_submission(self):
        profile_answers_before = deepcopy(self.store.profile("fixture").get("answers", []))
        application_before = self.store.application(self.application_id)
        review_path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        rehearsal_path = f"/profiles/fixture/applications/{self.application_id}/rehearsal"
        request_id = "criterion44-unknown-question"
        queued = self.client.post(review_path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]

        duplicate_admission = self.client.post(review_path, json={"request_id": request_id})
        self.assertEqual(duplicate_admission.status_code, 202, duplicate_admission.text)
        self.assertEqual(duplicate_admission.json()["operation_id"], operation_id)
        self.assertEqual(self._count(m.operations, kind="APPLICATION_FORM_REVIEW"), 1)

        html = (
            "<form><label for='custom_work_style'>What is your preferred work style?</label>"
            "<input id='custom_work_style' name='custom_work_style' type='text' required "
            "value='PRIVATE_UNAPPROVED_ANSWER'></form>"
        ).encode()
        fetched = []

        class FakeResponse:
            headers = {"content-type": "text/html; charset=utf-8"}
            content = html
            url = self.review_url

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check

            def get(self, url):
                self.check()
                fetched.append(url)
                if url != self.website or url != FakeResponse.url:
                    raise AssertionError("Worker attempted a non-fixture form route")
                return FakeResponse()

            def allowed(self, url):
                return url == self.website == FakeResponse.url

            def close(self):
                pass

        before_attempts = self._count(m.attempts, application_id=self.application_id)
        with patch.dict(os.environ, {
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root / "documents"),
        }), patch("workers.tasks.PublicCompanyClient", FakePublicClient), patch(
            "workers.tasks.apply_mock", side_effect=AssertionError("Submission adapter must not run"),
        ) as submit:
            worker_result = execute_operation(operation_id)

        self.assertEqual(worker_result["status"], "COMPLETED", worker_result)
        self.assertEqual(fetched, [self.review_url])
        submit.assert_not_called()
        self.assertEqual(self._count(m.attempts, application_id=self.application_id), before_attempts)
        self.assertEqual(self.store.application(self.application_id)["state"], application_before["state"])
        self.assertEqual(self.store.profile("fixture").get("answers", []), profile_answers_before)

        exact = self.client.get(f"{review_path}/{request_id}")
        latest = self.client.get(f"{review_path}/latest")
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(latest.status_code, 200, latest.text)
        saved = exact.json()
        self.assertEqual(saved, latest.json())
        self.assertEqual(saved["state"], "COMPLETED")
        self.assertEqual(saved["analysis"]["status"], "REVIEW_REQUIRED")
        self.assertTrue(any(
            field["review_reason"] == "UNKNOWN_QUESTION"
            for field in saved["analysis"]["fields"]
        ), saved["analysis"])
        self.assertNotIn("PRIVATE_UNAPPROVED_ANSWER", exact.text)
        self.assertNotIn('"value"', exact.text)

        audit_count = self._count(m.audit, entity_id=self.application_id, action="APPLICATION_FORM_REVIEW")
        self.assertEqual(audit_count, 1)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client(self.store)

        reopened_exact = self.client.get(f"{review_path}/{request_id}")
        reopened_latest = self.client.get(f"{review_path}/latest")
        self.assertEqual(reopened_exact.status_code, 200, reopened_exact.text)
        self.assertEqual(reopened_exact.json(), saved)
        self.assertEqual(reopened_latest.json(), saved)
        rehearsal = self.client.get(rehearsal_path)
        self.assertEqual(rehearsal.status_code, 200, rehearsal.text)
        preview = rehearsal.json()
        self.assertFalse(preview["ready"])
        self.assertFalse(preview["submission_enabled"])
        self.assertIn("FORM_REQUIRES_HUMAN_REVIEW", preview["blockers"])
        self.assertTrue(any(
            field["reason_code"] == "FORM_FIELD_REVIEW_REQUIRED" and field["value"] is None
            for field in preview["fields"]
        ), preview["fields"])

        replay = self.client.post(review_path, json={"request_id": request_id})
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json(), {
            "request_id": request_id, "operation_id": operation_id, "state": "COMPLETED",
        })
        self.assertEqual(self._count(m.operations, kind="APPLICATION_FORM_REVIEW"), 1)
        self.assertEqual(self._count(m.audit, entity_id=self.application_id,
                                    action="APPLICATION_FORM_REVIEW"), 1)
        self.assertEqual(fetched, [self.review_url])
        self.assertEqual(self._count(m.attempts, application_id=self.application_id), before_attempts)
        self.assertEqual(self.store.application(self.application_id)["state"], application_before["state"])

        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(
                requisition="engineering-456",
                url="https://jobs.ashbyhq.com/fixture/engineering-456",
            ))
        stale = self.client.post(review_path, json={"request_id": request_id})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "FORM_REVIEW_IDEMPOTENCY_CONFLICT")
        self.assertEqual(self._count(m.operations, kind="APPLICATION_FORM_REVIEW"), 1)
        self.assertEqual(self._count(m.audit, entity_id=self.application_id,
                                    action="APPLICATION_FORM_REVIEW"), 1)
        self.assertEqual(fetched, [self.review_url])
        self.assertEqual(self._count(m.attempts, application_id=self.application_id), before_attempts)
        self.assertEqual(self.store.application(self.application_id)["state"], application_before["state"])


if __name__ == "__main__":
    unittest.main()
