"""Synthetic Lever form-review API, worker, and durable safe readback."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from fixtures.mock_server import fixture_server
from workers.tasks import execute_operation


LEVER_URL = "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef"
_PRIVATE_VALUES = ("PRIVATE_LEVER_EMAIL", "PRIVATE_LEVER_SSN", "PRIVATE_LEVER_ANSWER")


class LeverFormReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-lever-review-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'lever-review.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(provider="lever", tenant="example",
                     requisition="01234567-89ab-cdef-0123-456789abcdef", url=LEVER_URL))
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

    def _path(self):
        return f"/profiles/fixture/applications/{self.application_id}/form-reviews"

    def _operation_count(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.operations).where(
                m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                m.operations.c.entity_id == self.application_id,
            )).scalar_one()

    def test_lever_route_persists_safe_review_metadata_by_request_id(self):
        request_id = "lever-review-request-001"
        # Admission freezes the canonical URL; a supported persisted /apply
        # alias must still pass worker staleness checks without a second route.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url=LEVER_URL + "/apply"))
        path = self._path()
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        fetches = []

        class FakeResponse:
            url = LEVER_URL
            headers = {"content-type": "text/html; charset=utf-8"}
            content = (
                b"<form>"
                b"<label for='email'>Email</label>"
                b"<input id='email' name='email' type='email' required value='PRIVATE_LEVER_EMAIL'>"
                b"<label for='ssn'>Social Security number</label>"
                b"<input id='ssn' name='ssn' required value='PRIVATE_LEVER_SSN'>"
                b"<input id='duplicate' name='first' value='PRIVATE_LEVER_ANSWER'>"
                b"<input id='duplicate' name='second' value='PRIVATE_LEVER_ANSWER'>"
                b"</form>"
            )

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check
                self.total_timeout = total_timeout

            def get(self, url):
                self.check()
                fetches.append(url)
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

        self.assertEqual(fetches, [LEVER_URL])
        self.assertEqual(result["status"], "COMPLETED")
        read = self.client.get(path + "/" + request_id)
        self.assertEqual(read.status_code, 200, read.text)
        payload = read.json()
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["state"], "COMPLETED")
        analysis = payload["analysis"]
        self.assertEqual(analysis["status"], "REVIEW_REQUIRED")
        self.assertEqual({field["name"] for field in analysis["fields"]},
                         {"email", "ssn", "first", "second"})
        review_reasons = {field["review_reason"] for field in analysis["fields"]}
        self.assertIn("SENSITIVE_FIELD", review_reasons)
        self.assertIn("AMBIGUOUS_FIELD", review_reasons)
        for secret in _PRIVATE_VALUES:
            self.assertNotIn(secret, read.text)

        with self.store.engine.connect() as connection:
            audits = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings())
        self.assertTrue(audits)
        serialized_audit = str([row["payload"] for row in audits])
        for secret in _PRIVATE_VALUES:
            self.assertNotIn(secret, serialized_audit)
        audit = next(row["payload"] for row in audits
                     if row["payload"].get("operation_id") == operation_id)
        self.assertEqual(audit["analysis"], analysis)
        cross_profile = self.client.get(
            f"/profiles/other-profile/applications/{self.application_id}/form-reviews/{request_id}"
        )
        self.assertEqual(cross_profile.status_code, 404)

    def test_malformed_or_unsupported_routes_do_not_enqueue_or_fetch(self):
        path = self._path()
        original_count = self._operation_count()

        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url="https://jobs.lever.co.attacker.test/example/01234567-89ab-cdef-0123-456789abcdef"))
        malformed = self.client.post(path, json={"request_id": "lever-malformed-route"})
        self.assertEqual(malformed.status_code, 409, malformed.text)
        self.assertEqual(self._operation_count(), original_count)

        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(provider="unsupported", url="https://unsupported.example/jobs/123"))
        unsupported = self.client.post(path, json={"request_id": "lever-unsupported-route"})
        self.assertEqual(unsupported.status_code, 409, unsupported.text)
        self.assertEqual(self._operation_count(), original_count)


if __name__ == "__main__":
    unittest.main()
