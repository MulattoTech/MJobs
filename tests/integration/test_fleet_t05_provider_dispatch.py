"""Connected public dispatch for the merged ATS form-review providers."""
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


class FleetT05ProviderDispatchTests(unittest.TestCase):
    ROUTES = (
        ("recruitee", "https://example.recruitee.com/o/software-engineer",
         "ANALYZED", None),
        ("smartrecruiters", "https://jobs.smartrecruiters.com/acme/123456789012-job",
         "REVIEW_REQUIRED", "DOM_INSPECTION_REQUIRED"),
        ("workable", "https://apply.workable.com/acme/j/ABC123/",
         "REVIEW_REQUIRED", "DOM_INSPECTION_REQUIRED"),
        ("teamtailor", "https://acme.teamtailor.com/jobs/123-job",
         "REVIEW_REQUIRED", "DOM_INSPECTION_REQUIRED"),
        ("bamboohr", "https://acme.bamboohr.com/careers/123",
         "REVIEW_REQUIRED", "FORM_SEMANTICS_UNSUPPORTED"),
        ("generic_html", "https://careers.example.test/job/123",
         "REVIEW_REQUIRED", "FORM_SEMANTICS_UNSUPPORTED"),
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t05-provider-dispatch-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'provider-dispatch.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_merged_routes_dispatch_and_persist_truthful_review_capabilities(self):
        markup = b"<form><input id='email' name='email' type='email'></form>"
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"

        for provider, route_url, expected_status, expected_reason in self.ROUTES:
            with self.subTest(provider=provider):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.store.application(self.application_id)["route_id"],
                    ).values(provider=provider, tenant=None, requisition=None, url=route_url))
                    connection.execute(update(m.applications).where(
                        m.applications.c.id == self.application_id,
                    ).values(target="REAL"))

                request_id = f"t05-{provider}"
                queued = self.client.post(path, json={"request_id": request_id})
                self.assertEqual(queued.status_code, 202, queued.text)
                operation_id = queued.json()["operation_id"]

                class FakeResponse:
                    headers = {"content-type": "text/html; charset=utf-8"}
                    content = markup

                class FakePublicClient:
                    def __init__(self, website, check=None, total_timeout=None):
                        self.website = website
                        self.check = check

                    def get(self, url):
                        self.check()
                        if url != self.website:
                            raise AssertionError("Dispatch changed the selected route")
                        response = FakeResponse()
                        response.url = url
                        return response

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
                exact = self.client.get(f"{path}/{request_id}")
                latest = self.client.get(path + "/latest")
                self.assertEqual(exact.status_code, 200, exact.text)
                self.assertEqual(latest.status_code, 200, latest.text)
                for response in (exact.json(), latest.json()):
                    self.assertEqual(response["state"], "COMPLETED")
                    self.assertEqual(response["analysis"]["status"], expected_status)
                    self.assertEqual(response["analysis"]["reason"], expected_reason)
                    self.assertNotIn("PRIVATE", str(response))

                with self.store.engine.connect() as connection:
                    operation = connection.execute(
                        m.operations.select().where(m.operations.c.id == operation_id),
                    ).mappings().one()
                self.assertEqual(operation["params"]["provider"], provider)
                self.assertEqual(operation["params"]["route_url"], route_url)

    def test_provider_route_mismatch_is_rejected_before_durable_queueing(self):
        application = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(
                provider="recruitee",
                tenant="example",
                requisition="software-engineer",
                url="https://jobs.smartrecruiters.com/acme/123456789012-job",
            ))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))

        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        rejected = self.client.post(path, json={"request_id": "t05-mismatched-route"})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["detail"]["code"], "FORM_REVIEW_ROUTE_UNSUPPORTED")
        with self.store.engine.connect() as connection:
            queued = connection.execute(select(m.operations).where(
                m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                m.operations.c.entity_id == self.application_id,
            )).all()
        self.assertEqual(queued, [])


if __name__ == "__main__":
    unittest.main()
