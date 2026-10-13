"""Known conditional controls need durable, input-bound consent before dry-run."""
from pathlib import Path
import copy
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class MockDynamicReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-dynamic-review-")
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = copy.deepcopy(PROFILE)
        profile["answers"].append({"id": "state", "question": "State or province",
            "text": "Oklahoma", "source": "fictional-test-profile", "verified": True,
            "user_approved": True, "confidence": 100})
        self.store.save_profile(profile)
        self.fixture = fixture_server()
        self.origin = self.fixture.__enter__()
        discover(self.store, self.origin, "fixture")
        self.application_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        with self.store.engine.begin() as connection:
            app = self.store.application(self.application_id)
            route = connection.execute(select(m.routes).where(m.routes.c.id == app["route_id"])).mappings().one()
            connection.execute(update(m.routes).where(m.routes.c.id == route["id"]).values(
                url=f"{self.origin}/ats/apply/{route['requisition']}?dynamic=known"))
        self.client = TestClient(create_app(self.store, self.origin, self.root, operation_mode="FIXTURE"))
        self.path = f"/profiles/fixture/applications/{self.application_id}/mock-dynamic-review"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.fixture.__exit__(None, None, None)
        self.temp.cleanup()

    def run_application(self, *, submit=False):
        return apply_mock(self.store, self.application_id, self.origin, self.root, submit=submit)

    def observe(self):
        self.assertEqual(self.run_application()["state"], "REVIEW_REQUIRED")
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertTrue(preview["ready"], preview)
        self.assertEqual([(field["label"], field["value"]) for field in preview["fields"]],
                         [("State or province", "Oklahoma")])
        return preview

    def approve(self, preview):
        request = {"request_id": "review-conditional-1", "fingerprint": preview["fingerprint"],
                   "acknowledged": True}
        response = self.client.post(self.path, json=request)
        self.assertEqual(response.status_code, 200, response.text)
        return request, response.json()

    def test_review_survives_reopen_and_resumes_to_dry_run_without_submission(self):
        preview = self.observe()
        self.assertIsNone(preview["latest_review"])
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})
        request, receipt = self.approve(preview)
        self.assertFalse(receipt["replayed"])
        self.assertTrue(self.client.post(self.path, json=request).json()["replayed"])
        self.client.close(); self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = TestClient(create_app(self.store, self.origin, self.root, operation_mode="FIXTURE"))
        self.assertEqual(self.client.get(self.path).json()["latest_review"]["fingerprint"], preview["fingerprint"])
        result = self.run_application()
        self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
        self.assertIsNone(result["confirmation"])
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts").json(), {"review": 1, "submit": 0})
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.audit.c.payload).where(m.audit.c.action == "MOCK_DYNAMIC_FIELDS_OBSERVED")).scalars().all()
        self.assertTrue(rows)
        # Option values are public form metadata, not the applicant's selection.
        self.assertTrue(all("value" not in field for row in rows for field in row["fields"]))
        self.assertNotIn(PROFILE["email"], __import__('json').dumps(rows))

    def test_dry_run_review_never_authorizes_even_mock_submission(self):
        self.approve(self.observe())
        self.assertEqual(self.run_application(submit=True)["state"], "REVIEW_REQUIRED")
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})

    def test_foreign_scope_unacknowledged_and_changed_route_cannot_reuse_review(self):
        preview = self.observe()
        foreign = self.path.replace("/fixture/", "/other/")
        self.assertEqual(self.client.get(foreign).status_code, 404)
        self.assertEqual(self.client.post(self.path, json={"request_id": "no-consent",
            "fingerprint": preview["fingerprint"], "acknowledged": False}).status_code, 422)
        self.approve(preview)
        with self.store.engine.begin() as connection:
            app = self.store.application(self.application_id)
            route = connection.execute(select(m.routes).where(m.routes.c.id == app["route_id"])).mappings().one()
            connection.execute(update(m.routes).where(m.routes.c.id == route["id"]).values(url=route["url"]+"&changed=1"))
        stale = self.client.get(self.path).json()
        self.assertFalse(stale["ready"])
        self.assertIsNone(stale["latest_review"])
        self.assertEqual(self.run_application()["state"], "REVIEW_REQUIRED")
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})

    def test_changed_document_or_current_profile_invalidates_saved_consent(self):
        preview = self.observe()
        self.approve(preview)
        with self.store.engine.begin() as connection:
            document = connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id)).mappings().first()
            connection.execute(update(m.documents).where(m.documents.c.id == document["id"]).values(
                sha256={**document["sha256"], "pdf": "0"*64}))
        self.assertFalse(self.client.get(self.path).json()["ready"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(m.documents.c.id == document["id"]).values(sha256=document["sha256"]))
        profile = self.store.profile("fixture")
        profile["answers"][-1]["user_approved"] = False
        self.store.save_profile(profile)
        current = self.client.get(self.path).json()
        self.assertFalse(current["ready"])
        self.assertIsNone(current["latest_review"])
        self.assertEqual(current["fields"], [])
        stale = self.client.post(self.path, json={"request_id": "stale-review", "fingerprint": preview["fingerprint"],
                                                "acknowledged": True})
        self.assertEqual(stale.status_code, 409)

    def test_changed_questionnaire_and_options_require_new_observation(self):
        from services.applications import mock_adapter
        self.approve(self.observe())
        original = mock_adapter._read_questionnaire
        def changed(client, route):
            questionnaire = original(client, route)
            questionnaire["revision"] += 1
            for field in questionnaire["questions"]:
                if field["id"] == "region":
                    field["options"] = ["Oklahoma", "Texas", "Kansas"]
            return questionnaire
        with patch.object(mock_adapter, "_read_questionnaire", changed):
            self.assertEqual(self.run_application()["state"], "REVIEW_REQUIRED")
        current = self.client.get(self.path).json()
        self.assertFalse(current["ready"])
        self.assertIsNone(current["latest_review"])
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})

    def test_unknown_fields_never_receive_conditional_consent(self):
        with self.store.engine.begin() as connection:
            app = self.store.application(self.application_id)
            route = connection.execute(select(m.routes).where(m.routes.c.id == app["route_id"])).mappings().one()
            connection.execute(update(m.routes).where(m.routes.c.id == route["id"]).values(
                url=route["url"].replace("dynamic=known", "dynamic=unknown")))
        self.assertEqual(self.run_application()["state"], "REVIEW_REQUIRED")
        self.assertFalse(self.client.get(self.path).json()["ready"])
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})
