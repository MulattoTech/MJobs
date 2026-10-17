"""Team 2 connected Workday DOM review and durable readback coverage."""
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from tests.integration.test_application_form_review_flow import ApplicationFormReviewFlowTests
from workers.tasks import execute_operation


class FleetT02WorkdayDynamicReviewTests(ApplicationFormReviewFlowTests):
    def test_t02_aria_dependent_questions_reach_durable_value_free_review(self):
        route_url = (
            "https://fixture.wd5.myworkdayjobs.com/en-US/Careers/job/"
            "Software_Engineer_R123"
        )
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(
                provider="workday", tenant="fixture", requisition="R123", url=route_url,
            ))

        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        request_id = "t02-workday-dynamic-review"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        html = (
            "<form><label for='country'>Country</label>"
            "<select id='country' aria-controls='region-question'></select>"
            "<section id='region-question'><label for='region'>Region</label>"
            "<input id='region' name='region' value='PRIVATE_REGION'></section></form>"
        ).encode()

        class FakeResponse:
            headers = {"content-type": "text/html; charset=utf-8"}
            content = html
            url = route_url

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check

            def get(self, url):
                self.check()
                if url != route_url:
                    raise AssertionError("Worker requested a different URL")
                return FakeResponse()

            def allowed(self, url):
                return url == route_url

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
        exact = self.client.get(path + "/" + request_id)
        latest = self.client.get(path + "/latest")
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(latest.status_code, 200, latest.text)
        expected = {
            "status": "REVIEW_REQUIRED",
            "reason": "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED",
            "fields": [
                {"id": "country", "name": None, "type": "select",
                 "required": False, "review_reason": "UNKNOWN_QUESTION"},
                {"id": "region", "name": "region", "type": "text",
                 "required": False, "review_reason": "UNKNOWN_QUESTION"},
            ],
        }
        self.assertEqual(exact.json()["analysis"], expected)
        self.assertEqual(latest.json()["analysis"], expected)

        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one()
            audits = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings())
        audit = next(row["payload"] for row in audits
                     if row["payload"].get("operation_id") == operation_id)
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual(audit["analysis"], expected)
        for payload in (exact.text, latest.text, str(operation["params"]), str(audit)):
            self.assertNotIn("PRIVATE_REGION", payload)
