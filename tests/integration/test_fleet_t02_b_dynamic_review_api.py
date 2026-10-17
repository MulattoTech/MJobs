"""T02 Helper B Workday dynamic-fields consumer freshness tests."""
from datetime import datetime, timezone
import hashlib
import json
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.application_form_review import _frozen_review_input, _input_fingerprint, _application_route
from apps.api.dynamic_application_fields import create_dynamic_application_fields_router
from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_dynamic_application_fields_api import DynamicApplicationFieldsApiTests
from workers.dispatch import enqueue_operation


def envelope(question_fingerprint="1" * 64):
    fields = [{
        "id": "region", "name": "region", "type": "text", "required": True,
        "question_fingerprint": question_fingerprint, "conditional": True,
        "conditional_on": "country", "repeatable": False, "repeatable_group": None,
    }, {
        "id": "country", "name": "country", "type": "select", "required": True,
        "question_fingerprint": "2" * 64, "conditional": False,
        "conditional_on": None, "repeatable": False, "repeatable_group": None,
    }]
    body = {"schema_version": 1, "fields": fields}
    fingerprint = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    return {**body, "fingerprint": fingerprint}


def base_field_projection(dynamic_review):
    return [{
        "id": item["id"],
        "name": item["name"],
        "type": item["type"],
        "required": item["required"],
        "review_reason": "UNKNOWN_QUESTION",
    } for item in dynamic_review["fields"]]


class FleetT02BDynamicReviewApiTests(unittest.TestCase):
    def _client(self):
        return DynamicApplicationFieldsApiTests._client(self)

    def setUp(self):
        DynamicApplicationFieldsApiTests.setUp(self)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(
                provider="workday", tenant="fixture", requisition="R123",
                url="https://fixture.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R123",
            ))
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")
        app.include_router(create_dynamic_application_fields_router(self.store))
        self.client.close()
        self.client = TestClient(app)

    def tearDown(self):
        DynamicApplicationFieldsApiTests.tearDown(self)

    def _save_review(self, request_id, dynamic_review, state="COMPLETED",
                     admitted_at=None, updated_at=None, receipt_request_id=None,
                     receipt_input_fingerprint=None, analysis=None,
                     duplicate_receipt=False):
        row, route = _application_route(self.store, "fixture", self.application_id)
        frozen = _frozen_review_input(self.store, "fixture", self.application_id, row, route)
        params = {**frozen, "request_id": request_id}
        params["input_fingerprint"] = _input_fingerprint(frozen)
        params["admitted_at"] = (admitted_at or datetime.now(timezone.utc)).isoformat()
        operation_id = enqueue_operation(
            self.store, "APPLICATION_FORM_REVIEW", self.application_id, params)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id,
            ).values(state=state, updated_at=updated_at or datetime.now(timezone.utc)))
            if state == "COMPLETED":
                receipt = {
                    "operation_id": operation_id,
                    "profile_id": params["profile_id"],
                    "route_id": params["route_id"],
                    "provider": params["provider"],
                    "request_id": receipt_request_id or request_id,
                    "input_fingerprint": receipt_input_fingerprint or params["input_fingerprint"],
                    "analysis": analysis or {
                        "status": "REVIEW_REQUIRED",
                        "reason": "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED",
                        "fields": base_field_projection(dynamic_review),
                        "dynamic_review": dynamic_review,
                    },
                }
                self.store._audit(connection, self.application_id, "APPLICATION_FORM_REVIEW", receipt)
                if duplicate_receipt:
                    self.store._audit(connection, self.application_id,
                                      "APPLICATION_FORM_REVIEW", receipt)
        return operation_id

    def test_latest_review_fingerprint_survives_store_restart_and_question_change_stales_it(self):
        first = envelope()
        self._save_review("t02-b-before-restart", first)
        expected = first["fingerprint"]
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")
        app.include_router(create_dynamic_application_fields_router(self.store))
        self.client = TestClient(app)

        readback = self.client.get(self.url, params={"expected_fingerprint": expected})
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["fingerprint"], expected)
        self.assertEqual(readback.json()["status"], "REVIEW_REQUIRED")
        self.assertFalse(readback.json()["fields"][0]["supported"])

        changed = envelope("3" * 64)
        self._save_review("t02-b-changed-question", changed)
        stale = self.client.get(self.url, params={"expected_fingerprint": expected})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")

    def test_pending_or_cancelled_latest_refresh_never_falls_back_to_old_review(self):
        complete = envelope()
        self._save_review("t02-b-complete", complete)
        for suffix, state in (("pending", "QUEUED"), ("cancelled", "CANCELLED")):
            with self.subTest(state=state):
                self._save_review(f"t02-b-refresh-{suffix}", envelope("4" * 64), state)
                stale = self.client.get(self.url, params={
                    "expected_fingerprint": complete["fingerprint"],
                })
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(stale.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")

    def test_newer_admission_wins_when_old_worker_updates_afterward(self):
        older_admission = datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)
        newer_admission = datetime(2026, 10, 17, 4, 1, tzinfo=timezone.utc)
        old = envelope()
        self._save_review("t02-b-old", old, admitted_at=older_admission,
                          updated_at=datetime(2026, 10, 17, 4, 10, tzinfo=timezone.utc))
        self._save_review("t02-b-new-pending", envelope("5" * 64), "QUEUED",
                          admitted_at=newer_admission,
                          updated_at=datetime(2026, 10, 17, 4, 0, 30, tzinfo=timezone.utc))
        stale = self.client.get(self.url, params={"expected_fingerprint": old["fingerprint"]})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")

    def test_route_or_profile_input_drift_invalidates_review_fingerprint(self):
        first = envelope()
        self._save_review("t02-b-route-bound", first)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(
                url="https://fixture.wd5.myworkdayjobs.com/en-US/Careers/job/Other_R124",
                requisition="R124",
            ))
        route_stale = self.client.get(self.url, params={
            "expected_fingerprint": first["fingerprint"],
        })
        self.assertEqual(route_stale.status_code, 409, route_stale.text)

        second = envelope("6" * 64)
        self._save_review("t02-b-profile-bound", second)
        from tests.integration.test_spine import PROFILE
        self.store.save_profile({**PROFILE, "name": "Changed Synthetic Profile"})
        profile_stale = self.client.get(self.url, params={
            "expected_fingerprint": second["fingerprint"],
        })
        self.assertEqual(profile_stale.status_code, 409, profile_stale.text)

    def test_invalid_or_transplanted_review_receipt_fails_closed_without_echo(self):
        request_fingerprint = envelope()["fingerprint"]
        for suffix, receipt_options in (
            ("request", {"receipt_request_id": "other-request"}),
            ("input", {"receipt_input_fingerprint": "f" * 64}),
            ("duplicate", {"duplicate_receipt": True}),
            ("envelope", {"analysis": {
                "status": "REVIEW_REQUIRED", "reason": "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED",
                "fields": base_field_projection(envelope()),
                "dynamic_review": {
                    **envelope(), "PRIVATE_RECEIPT_CANARY": "secret",
                },
            }}),
        ):
            with self.subTest(receipt=suffix):
                self._save_review(f"t02-b-invalid-receipt-{suffix}", envelope(),
                                  **receipt_options)
                response = self.client.get(self.url, params={
                    "expected_fingerprint": request_fingerprint,
                })
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")
                self.assertNotIn("PRIVATE_RECEIPT_CANARY", response.text)


if __name__ == "__main__":
    unittest.main()
