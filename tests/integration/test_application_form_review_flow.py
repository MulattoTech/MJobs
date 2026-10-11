"""Connected form-review API, durable worker, and safe audit readback."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class ApplicationFormReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-form-review-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'form-review.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(self.application_id)
        self.route_url = "https://boards.greenhouse.io/example/jobs/12345"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(provider="greenhouse", tenant="example", requisition="12345", url=self.route_url))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_explicit_request_runs_read_only_review_and_persists_only_safe_result(self):
        request_id = "review-request-001"
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]

        class FakeResponse:
            url = self.route_url
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"<form><label for='email'>Email</label><input id='email' name='email' type='email' required value='PRIVATE_VALUE'><input id='ssn' name='ssn' type='text'></form>"

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check
                self.total_timeout = total_timeout
            def get(self, url):
                self.check()
                if url != self.website:
                    raise AssertionError("Worker requested a different URL")
                return FakeResponse()
            def allowed(self, url):
                return url == self.website
            def close(self):
                pass

        with patch.dict("os.environ", {
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root / "documents"),
        }), patch("workers.tasks.PublicCompanyClient", FakePublicClient):
            result = execute_operation(operation_id)

        self.assertEqual(result["status"], "COMPLETED")
        read = self.client.get(path + "/" + request_id)
        self.assertEqual(read.status_code, 200, read.text)
        payload = read.json()
        self.assertEqual(payload["state"], "COMPLETED")
        self.assertEqual(payload["analysis"]["status"], "REVIEW_REQUIRED")
        self.assertTrue(any(field["review_reason"] == "SENSITIVE_FIELD" for field in payload["analysis"]["fields"]))
        self.assertNotIn("PRIVATE_VALUE", read.text)
        self.assertNotIn("value", read.text.casefold())
        self.assertEqual(self.client.get(
            f"/profiles/other-profile/applications/{self.application_id}/form-reviews/{request_id}"
        ).status_code, 404)

    def test_unsupported_or_malformed_requests_never_queue_a_fetch(self):
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        malformed = self.client.post(path, json={"request_id": "bad id", "submit": True})
        self.assertEqual(malformed.status_code, 422)
        wrong_profile = self.client.post(
            f"/profiles/missing/applications/{self.application_id}/form-reviews",
            json={"request_id": "review-request-002"},
        )
        self.assertEqual(wrong_profile.status_code, 404)


if __name__ == "__main__":
    unittest.main()
