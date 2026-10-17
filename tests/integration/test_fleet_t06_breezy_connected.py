"""Synthetic connected API, worker, and durable Breezy review acceptance."""
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import create_app
from packages.persistence import models as m
from services.applications.adapters.breezy import BreezyApplicationAdapter
from services.applications.form_review_providers import normalize_application_route
from tests.integration import test_application_form_review_flow as base_flow
from workers.tasks import execute_operation


_ROUTE_URL = "https://fixture.breezy.hr/p/012345abcdef-engineer"
_TENANT = "fixture"
_REQUISITION = "012345abcdef"
_PRIVATE_VALUES = (
    "PRIVATE_BREEZY_EMAIL",
    "PRIVATE_BREEZY_ANSWER",
    "PRIVATE_BREEZY_OPTION",
    "PRIVATE_BREEZY_LABEL",
    "PRIVATE_BREEZY_ARIA",
    "PRIVATE_BREEZY_PLACEHOLDER",
    "PRIVATE_BREEZY_CONTROL_ID",
    "PRIVATE_BREEZY_CONTROL_NAME",
    "PRIVATE_BREEZY_OPTION_ID",
    "PRIVATE_BREEZY_OPTION_NAME",
)
_FORM_HTML = (
    "<form>"
    "<label for='email'>Email address</label>"
    "<input id='email' name='email' type='email' required "
    "value='PRIVATE_BREEZY_EMAIL'>"
    "<label for='PRIVATE_BREEZY_CONTROL_ID'>PRIVATE_BREEZY_LABEL</label>"
    "<input id='PRIVATE_BREEZY_CONTROL_ID' name='PRIVATE_BREEZY_CONTROL_NAME' type='text' "
    "aria-label='PRIVATE_BREEZY_ARIA' "
    "placeholder='PRIVATE_BREEZY_PLACEHOLDER' "
    "value='PRIVATE_BREEZY_ANSWER'>"
    "<label for='choice'>Preference</label>"
    "<select id='PRIVATE_BREEZY_OPTION_ID' name='PRIVATE_BREEZY_OPTION_NAME'><option>"
    "PRIVATE_BREEZY_OPTION</option></select>"
    "</form>"
).encode("utf-8")


class FleetT06BreezyConnectedTests(TestCase):
    """Use the established fixture lifecycle without collecting its test class."""

    def setUp(self):
        self.flow = base_flow.ApplicationFormReviewFlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.tearDown)
        self.store = self.flow.store
        self.root = self.flow.root
        self.database_url = self.flow.database_url
        self.application_id = self.flow.application_id
        self._set_route()
        self.normalized_route = normalize_application_route(
            "breezy", _ROUTE_URL, route_metadata={
                "provider": "breezy",
                "tenant": _TENANT,
                "requisition": _REQUISITION,
                "url": _ROUTE_URL,
            },
        )
        self.assertIsNotNone(self.normalized_route)

    def _set_route(self, **values):
        route_id = self.store.application(self.application_id)["route_id"]
        route_values = {
            "provider": "breezy",
            "tenant": _TENANT,
            "requisition": _REQUISITION,
            "url": _ROUTE_URL,
        }
        route_values.update(values)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == route_id,
            ).values(**route_values))

    def _path(self):
        return f"/profiles/fixture/applications/{self.application_id}/form-reviews"

    def _review_audits(self, store=None):
        store = store or self.store
        with store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(
                m.audit.c.payload,
            ).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings()]

    def _attempt_count(self, store=None):
        store = store or self.store
        with store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(
                m.attempts,
            ).where(m.attempts.c.application_id == self.application_id)).scalar_one()

    def _operation(self, operation_id, store=None):
        store = store or self.store
        with store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one())

    def _execute(self, operation_id, *, html=_FORM_HTML):
        fetched = []
        normalized_url = self.normalized_route["url"]

        class FakeResponse:
            def __init__(self):
                self.url = normalized_url
                self.headers = {"content-type": "text/html; charset=utf-8"}
                self.content = html

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check
                self.total_timeout = total_timeout

            def get(self, url):
                if self.check is not None:
                    self.check()
                fetched.append(url)
                if url != self.website or url != normalized_url:
                    raise AssertionError("Worker requested a route outside the synthetic fixture")
                return FakeResponse()

            def allowed(self, url):
                return url == normalized_url

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
        return result, fetched

    def _admit(self, request_id):
        response = self.flow.client.post(self._path(), json={"request_id": request_id})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["operation_id"]

    def test_breezy_review_runs_real_adapter_and_survives_sqlite_reopen(self):
        request_id = "t06-breezy-durable-review"
        operation_id = self._admit(request_id)
        operation = self._operation(operation_id)
        self.assertEqual(operation["params"]["provider"], "breezy")
        self.assertEqual(operation["params"]["route_url"], self.normalized_route["url"])
        analyze = BreezyApplicationAdapter.analyze_form
        calls = []

        def analyze_and_record(adapter, html, *, route_url):
            calls.append(route_url)
            return analyze(adapter, html, route_url=route_url)

        with patch.object(BreezyApplicationAdapter, "analyze_form", autospec=True,
                          side_effect=analyze_and_record) as analyzer:
            result, fetched = self._execute(operation_id)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(fetched, [self.normalized_route["url"]])
        analyzer.assert_called_once()
        self.assertEqual(calls, [self.normalized_route["url"]])

        # Reopen the actual SQLite file with a fresh Store and API application.
        self.flow.client.close()
        self.store.engine.dispose()
        reopened = self.flow._new_store()
        self.flow.store = reopened
        self.store = reopened
        self.flow.client = TestClient(create_app(
            reopened, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

        exact = self.flow.client.get(self._path() + "/" + request_id)
        latest = self.flow.client.get(self._path() + "/latest")
        explicit = self.flow.client.get(self._path() + "/requests/" + request_id)
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(explicit.status_code, 200, explicit.text)
        self.assertEqual(exact.json(), latest.json())
        self.assertEqual(exact.json(), explicit.json())
        self.assertEqual(exact.json()["request_id"], request_id)
        for payload in (exact.json(), latest.json(), explicit.json()):
            self.assertEqual(payload["state"], "COMPLETED")
            self.assertEqual(payload["analysis"]["status"], "REVIEW_REQUIRED")
            self.assertEqual(payload["analysis"]["reason"], "MANUAL_REVIEW_REQUIRED")
            email = next(field for field in payload["analysis"]["fields"]
                         if field["name"] == "email")
            self.assertEqual(email["type"], "email")
            self.assertTrue(email["required"])
            self.assertEqual(email["review_reason"], "UNKNOWN_QUESTION")

        audits = self._review_audits(reopened)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["payload"]["operation_id"], operation_id)
        self.assertEqual(audits[0]["payload"]["analysis"], exact.json()["analysis"])
        persisted_operation = self._operation(operation_id, reopened)
        self.assertEqual(persisted_operation["params"]["provider"], "breezy")
        self.assertEqual(persisted_operation["params"]["route_url"],
                         self.normalized_route["url"])
        self.assertEqual(self._attempt_count(reopened), 0)
        for secret in _PRIVATE_VALUES:
            for response in (exact, latest, explicit):
                self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, str(audits[0]["payload"]))

    def test_reserved_latest_request_id_has_explicit_historical_readback(self):
        operation_id = self._admit("latest")
        result, fetched = self._execute(operation_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(fetched, [self.normalized_route["url"]])

        explicit = self.flow.client.get(self._path() + "/requests/latest")
        current = self.flow.client.get(self._path() + "/latest")
        self.assertEqual(explicit.status_code, 200, explicit.text)
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(explicit.json()["request_id"], "latest")
        self.assertEqual(explicit.json()["state"], "COMPLETED")
        self.assertEqual(explicit.json()["analysis"], current.json()["analysis"])
        self.assertEqual(explicit.json()["analysis"]["status"], "REVIEW_REQUIRED")
        self.assertEqual(explicit.json()["analysis"]["reason"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(len(self._review_audits()), 1)
        for secret in _PRIVATE_VALUES:
            self.assertNotIn(secret, explicit.text)
            self.assertNotIn(secret, str(self._review_audits()[0]["payload"]))

    def test_breezy_rejects_mismatched_or_ambiguous_route_identity_before_queue(self):
        cases = (
            {"tenant": "other-tenant"},
            {"requisition": "fedcba987654"},
            {"url": _ROUTE_URL + "?source=fixture\r\n"},
            {"url": "https://fixture.breezy.hr/p/012345abcdef-job?x=1\r\n"},
        )
        for index, route_change in enumerate(cases):
            with self.subTest(route_change=route_change):
                self._set_route(**route_change)
                response = self.flow.client.post(
                    self._path(), json={"request_id": f"t06-breezy-invalid-{index}"},
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"],
                                 "FORM_REVIEW_ROUTE_UNSUPPORTED")
                with self.store.engine.connect() as connection:
                    queued = connection.execute(select(m.operations.c.id).where(
                        m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                        m.operations.c.entity_id == self.application_id,
                    )).all()
                self.assertEqual(queued, [])
                self.assertEqual(self._review_audits(), [])
                self.assertEqual(self._attempt_count(), 0)
                # Restore the valid synthetic identity for the next case.
                self._set_route()

    def test_breezy_profile_or_route_drift_before_worker_never_fetches_or_audits(self):
        for drift in ("profile", "route"):
            with self.subTest(drift=drift):
                self._set_route()
                request_id = f"t06-breezy-drift-{drift}"
                operation_id = self._admit(request_id)
                if drift == "profile":
                    profile = self.store.profile("fixture")
                    profile["name"] = "Edited Synthetic Applicant"
                    self.store.save_profile(profile, expected_version=profile["version"])
                else:
                    route_id = self.store.application(self.application_id)["route_id"]
                    with self.store.engine.begin() as connection:
                        connection.execute(update(m.routes).where(
                            m.routes.c.id == route_id,
                        ).values(url="https://fixture.breezy.hr/p/fedcba987654-engineer",
                                 requisition="fedcba987654"))

                result, fetched = self._execute(operation_id)
                self.assertEqual(result["status"], "DEAD_LETTERED")
                self.assertEqual(fetched, [])
                self.assertEqual(self._review_audits(), [])
                operation = self._operation(operation_id)
                self.assertEqual(operation["state"], "DEAD_LETTERED")
                self.assertEqual(operation["error"], "FORM_REVIEW_INPUT_STALE")
                exact = self.flow.client.get(self._path() + "/" + request_id)
                explicit = self.flow.client.get(
                    self._path() + "/requests/" + request_id,
                )
                for response in (exact, explicit):
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["state"], "DEAD_LETTERED")
                    self.assertEqual(response.json()["error_code"],
                                     "FORM_REVIEW_INPUT_STALE")
                    self.assertIsNone(response.json()["analysis"])
                latest = self.flow.client.get(self._path() + "/latest")
                self.assertEqual(latest.status_code, 200, latest.text)
                self.assertEqual(latest.json()["state"], "UNKNOWN")
                self.assertIsNone(latest.json()["analysis"])
                self.assertEqual(self._attempt_count(), 0)

    def test_breezy_cancelled_before_worker_execution_has_no_fetch_or_review_receipt(self):
        operation_id = self._admit("t06-breezy-cancel-before-run")
        cancelled = self.flow.client.post(f"/operations/{operation_id}/cancel")
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        self.assertEqual(cancelled.json()["state"], "CANCELLED")
        self.assertTrue(cancelled.json()["cancel_requested"])
        persisted = self._operation(operation_id)
        self.assertEqual(persisted["state"], "CANCELLED")
        self.assertTrue(persisted["cancel_requested"])
        result, fetched = self._execute(operation_id)
        self.assertEqual(result["status"], "ALREADY_CLAIMED_OR_CAPACITY")
        self.assertEqual(fetched, [])
        self.assertEqual(self._review_audits(), [])
        latest = self.flow.client.get(self._path() + "/latest")
        exact = self.flow.client.get(self._path() + "/t06-breezy-cancel-before-run")
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(exact.status_code, 200, exact.text)
        for response in (latest, exact):
            self.assertEqual(response.json()["state"], "CANCELLED")
            self.assertIsNone(response.json()["analysis"])
        self.assertEqual(self._attempt_count(), 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
