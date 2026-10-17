"""Team 2 public Workday review contract; synthetic values never leave fixtures."""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from tests.integration import test_application_form_review_flow as review_fixture
from workers.tasks import execute_operation


ROUTE = "https://fixture.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R123"


def form(label="Region", controller="country", row="1"):
    return (
        "<form><label for='country'>Country</label>"
        "<select id='country' name='country' data-workday-conditional='false' "
        "data-workday-repeatable='false'><option selected>PRIVATE_CHOICE</option></select>"
        "<label for='alternate'>Alternate country</label>"
        "<select id='alternate' name='alternate' data-workday-conditional='false' "
        "data-workday-repeatable='false'></select>"
        f"<label for='region'>{label}</label><input id='region' name='region' "
        f"data-workday-conditional='true' data-workday-conditional-on='{controller}' "
        "data-workday-repeatable='false' required value='PRIVATE_ANSWER'>"
        f"<label for='employer-{row}'>Employer</label><input id='employer-{row}' "
        f"name='employer-{row}' data-workday-repeatable='true' "
        "data-workday-repeatable-group='experience' data-workday-conditional='false' "
        "value='PRIVATE_EMPLOYER'></form>"
    ).encode()


class FleetT02WorkdayDynamicReviewTests(unittest.TestCase):
    setUp = review_fixture.ApplicationFormReviewFlowTests.setUp
    tearDown = review_fixture.ApplicationFormReviewFlowTests.tearDown
    _new_store = review_fixture.ApplicationFormReviewFlowTests._new_store

    def _review(self, request_id, markup):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(provider="workday", tenant="fixture", requisition="R123", url=ROUTE))
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)

        class Response:
            url = ROUTE
            headers = {"content-type": "text/html; charset=utf-8"}
            content = markup

        class PublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.check = check

            def get(self, url):
                if url != ROUTE:
                    raise AssertionError("Unexpected fixture URL")
                self.check()
                return Response()

            def allowed(self, url):
                return url == ROUTE

            def close(self):
                pass

        with patch.dict("os.environ", {
            "MJOBS_OPERATION_MODE": "FIXTURE", "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root / "documents"),
        }), patch("workers.tasks.PublicCompanyClient", PublicClient):
            result = execute_operation(queued.json()["operation_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        response = self.client.get(path + "/" + request_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "COMPLETED")
        self.assertEqual(self.client.get(path + "/latest").json()["analysis"],
                         response.json()["analysis"])
        return response.json()["analysis"]

    def test_public_review_retains_dynamic_structure_and_excludes_values(self):
        analysis = self._review("t02-structure", form())
        self.assertEqual(analysis["status"], "REVIEW_REQUIRED")
        dynamic = analysis["dynamic_review"]
        self.assertEqual(dynamic["schema_version"], 1)
        self.assertRegex(dynamic["fingerprint"], r"^[a-f0-9]{64}$")
        by_id = {field["id"]: field for field in dynamic["fields"]}
        self.assertTrue(by_id["region"]["conditional"])
        self.assertEqual(by_id["region"]["conditional_on"], "country")
        self.assertEqual(by_id["employer-1"]["repeatable_group"], "experience")
        with self.store.engine.connect() as connection:
            audit = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).scalars())
        serialized = json.dumps({"analysis": analysis, "audit": audit})
        for marker in ("PRIVATE_CHOICE", "PRIVATE_ANSWER", "PRIVATE_EMPLOYER"):
            self.assertNotIn(marker, serialized)

    def test_changed_question_dependency_and_row_replace_fingerprint(self):
        original = self._review("t02-original", form())["dynamic_review"]["fingerprint"]
        for request_id, markup in (
            ("t02-label", form(label="Province")),
            ("t02-dependent", form(controller="alternate")),
            ("t02-row", form(row="2")),
        ):
            with self.subTest(request_id=request_id):
                updated = self._review(request_id, markup)
                self.assertNotEqual(updated["dynamic_review"]["fingerprint"], original)

    def test_dynamic_fields_readback_survives_restart_and_rejects_stale_review(self):
        first = self._review("t02-before-restart", form())
        expected = first["dynamic_review"]["fingerprint"]
        self.client.close()
        self.store.engine.dispose()
        self.store = self._new_store()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))
        path = f"/profiles/fixture/applications/{self.application_id}/dynamic-fields"
        readback = self.client.get(path, params={"expected_fingerprint": expected})
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["fingerprint"], expected)
        self.assertEqual(readback.json()["status"], "REVIEW_REQUIRED")
        self._review("t02-after-restart", form(label="Province"))
        stale = self.client.get(path, params={"expected_fingerprint": expected})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.client.get(path.replace("/fixture/", "/other/"))
                         .status_code, 404)

    def test_pending_and_cancelled_refresh_never_reuse_prior_fingerprint(self):
        first = self._review("t02-complete-before-interruption", form())
        expected = first["dynamic_review"]["fingerprint"]
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        pending = self.client.post(path, json={"request_id": "t02-interrupted-refresh"})
        self.assertEqual(pending.status_code, 202, pending.text)
        fields = path.replace("/form-reviews", "/dynamic-fields")
        self.assertEqual(self.client.get(fields, params={
            "expected_fingerprint": expected,
        }).status_code, 409)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == pending.json()["operation_id"],
            ).values(state="CANCELLED", cancel_requested=True))
        self.assertEqual(self.client.get(fields, params={
            "expected_fingerprint": expected,
        }).status_code, 409)
