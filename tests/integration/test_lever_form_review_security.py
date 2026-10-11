"""Security-focused synthetic API acceptance for durable Lever form review."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


LEVER_URL = "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef"
PRIVATE_MARKERS = (
    "PRIVATE_LEVER_EMAIL", "PRIVATE_LEVER_ANSWER", "PRIVATE_LEVER_SSN",
    "PRIVATE_LEVER_PASSWORD", "PRIVATE_REQUEST_VALUE", "PRIVATE_REQUEST_CREDENTIAL",
)


class LeverFormReviewSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-lever-review-security-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'review-security.db').as_posix()}"
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
        self.client = self._new_client()

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _new_client(self):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def _path(self, profile_id="fixture"):
        return f"/profiles/{profile_id}/applications/{self.application_id}/form-reviews"

    def _operation_count(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.operations).where(
                m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                m.operations.c.entity_id == self.application_id,
            )).scalar_one()

    def _set_route(self, *, provider="lever", url=LEVER_URL):
        route_id = self.store.application(self.application_id)["route_id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                provider=provider, url=url,
            ))

    def test_cross_profile_and_untrusted_request_fields_are_denied_without_persistence(self):
        count_before = self._operation_count()
        body = {
            "request_id": "lever-security-body-rejected",
            "values": {"email": "PRIVATE_REQUEST_VALUE"},
            "credentials": {"password": "PRIVATE_REQUEST_CREDENTIAL"},
        }
        response = self.client.post(self._path(), json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._operation_count(), count_before)
        self.assertNotIn("PRIVATE_REQUEST_VALUE", response.text)
        self.assertNotIn("PRIVATE_REQUEST_CREDENTIAL", response.text)

        cross_profile_post = self.client.post(
            self._path("other-profile"), json={"request_id": "lever-cross-profile-post"},
        )
        self.assertEqual(cross_profile_post.status_code, 404)
        latest = self.client.get(
            f"/profiles/other-profile/applications/{self.application_id}/form-reviews/latest"
        )
        self.assertEqual(latest.status_code, 404)
        self.assertEqual(self._operation_count(), count_before)

    def test_malformed_and_unsupported_routes_fail_closed_before_fetch_or_queue(self):
        path = self._path()
        count_before = self._operation_count()
        fetches = []

        class MustNotFetchClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def get(self, url):
                fetches.append(url)
                raise AssertionError("unsupported route attempted public fetch")

            def close(self):
                pass

        cases = (
            ("lever", "https://jobs.lever.co.attacker.test/example/01234567-89ab-cdef-0123-456789abcdef"),
            ("unknown", "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef"),
        )
        with patch("workers.tasks.PublicCompanyClient", MustNotFetchClient):
            for index, (provider, route_url) in enumerate(cases):
                with self.subTest(provider=provider):
                    self._set_route(provider=provider, url=route_url)
                    response = self.client.post(path, json={"request_id": f"lever-bad-route-{index}"})
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertIn("FORM_REVIEW_ROUTE_UNSUPPORTED", response.text)
                    self.assertEqual(self._operation_count(), count_before)
        self.assertEqual(fetches, [])

    def test_safe_result_is_durable_latest_after_api_reload_and_excludes_values_and_credentials(self):
        request_id = "lever-review-durable-latest"
        path = self._path()
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        fetches = []
        html = (
            b"<form>"
            b"<label for='email'>Email</label>"
            b"<input id='email' name='email' type='email' required value='PRIVATE_LEVER_EMAIL'>"
            b"<label for='ssn'>Social Security number</label>"
            b"<input id='ssn' name='ssn' value='PRIVATE_LEVER_SSN'>"
            b"<input id='note' name='note' value='PRIVATE_LEVER_ANSWER'>"
            b"</form>"
        )

        class FakeResponse:
            url = LEVER_URL
            headers = {"content-type": "text/html; charset=utf-8"}
            content = html

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check

            def get(self, url):
                self.check()
                fetches.append(url)
                if url != self.website:
                    raise AssertionError("worker requested an unexpected route")
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
        by_request = self.client.get(path + "/" + request_id)
        self.assertEqual(by_request.status_code, 200, by_request.text)
        report = by_request.json()
        self.assertEqual(report["state"], "COMPLETED")
        self.assertEqual(report["analysis"]["status"], "REVIEW_REQUIRED")
        self.assertEqual({field["name"] for field in report["analysis"]["fields"]},
                         {"email", "ssn", "note"})

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.store.migrate()
        self.client = self._new_client()
        latest_url = path + "/latest"
        latest = self.client.get(latest_url)
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json(), report)

        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one()
            audits = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings())
        stored = str({"params": operation["params"], "audits": [row["payload"] for row in audits]})
        returned = latest.text + by_request.text + queued.text
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, stored)
            self.assertNotIn(marker, returned)
        self.assertNotIn("credentials", stored.casefold())
        self.assertNotIn("value", str(report["analysis"]).casefold())


if __name__ == "__main__":
    unittest.main()
