"""Acceptance tests for the root-mounted dynamic application fields endpoint."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class DynamicApplicationFieldsAppMountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-dynamic-fields-app-mount-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'dynamic-fields.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        # Deliberately rely on create_app's registered routers; do not mount this route here.
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")
        self.client = TestClient(app)
        self.url = f"/profiles/fixture/applications/{self.application_id}/dynamic-fields"

    def _report(self, *, result="PASS", mappings=None):
        application = self.store.application(self.application_id)
        return {
            "result": result,
            "reason": "DYNAMIC_REQUIRED_FIELDS_REVIEW_REQUIRED" if result != "PASS" else None,
            "observed_at": "2026-10-09T00:00:00Z",
            "profile_version": application["profile_version"],
            "document_version": application["document_version"],
            "route_id": application["route_id"],
            "metadata_fingerprint": "e" * 64,
            "mappings": mappings if mappings is not None else [
                {
                    "field_id": "country", "question": "Country", "type": "select",
                    "required": True, "visible": True, "depends_on": None,
                    "options": ["United States", "Canada"], "repeatable": False,
                    "type_explicit": True, "sensitivity": "STANDARD", "ambiguous": False,
                    "supported": True, "manual_review_required": False,
                    "current_value": "PRIVATE_ANSWER",
                },
                {
                    "field_id": "region", "question": "State or province", "type": "select",
                    "required": True, "visible": True, "depends_on": "country",
                    "options": ["Oklahoma", "Texas"], "repeatable": False,
                    "type_explicit": True, "sensitivity": "STANDARD", "ambiguous": False,
                    "supported": True, "manual_review_required": False,
                    "condition": {"question_id": "country", "equals": "United States"},
                },
            ],
        }

    def _persist(self, report):
        with self.store.engine.begin() as connection:
            self.store._audit(connection, self.application_id,
                              "APPLICATION_QUESTION_PREFLIGHT", report)

    def _preflight_count(self):
        with self.store.engine.connect() as connection:
            return len(connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
            )).all())

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_valid_field_metadata_is_available_through_create_app_mount(self):
        self._persist(self._report())
        before = self._preflight_count()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["application_id"], self.application_id)
        self.assertRegex(body["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(body["fields"], [
            {"id": "country", "label": "Country", "type": "select",
             "required": True, "visible": True, "depends_on": None,
             "options": ["United States", "Canada"], "repeatable": False,
             "supported": True, "sensitive": False, "manual_review_required": False},
            {"id": "region", "label": "State or province", "type": "select",
             "required": True, "visible": True, "depends_on": "country",
             "options": ["Oklahoma", "Texas"], "repeatable": False,
             "supported": True, "sensitive": False, "manual_review_required": False,
             "condition": {"question_id": "country", "equals": "United States"}},
        ])
        self.assertEqual(self._preflight_count(), before)
        self.assertNotIn("PRIVATE_ANSWER", response.text)
        self.assertNotIn("current_value", response.text)

    def test_review_required_keeps_safe_new_field_metadata_visible(self):
        self._persist(self._report(result="UNKNOWN"))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual([field["id"] for field in response.json()["fields"]], [
            "country", "region",
        ])
        self.assertNotIn("PRIVATE_ANSWER", response.text)

    def test_sensitive_field_is_visible_only_as_manual_review(self):
        report = self._report()
        report["mappings"][0]["sensitivity"] = "SENSITIVE"
        report["mappings"][0]["ambiguous"] = True
        self._persist(report)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        country = next(field for field in response.json()["fields"] if field["id"] == "country")
        self.assertTrue(country["manual_review_required"])
        self.assertFalse(country["supported"])
        self.assertNotIn("PRIVATE_ANSWER", response.text)

    def test_malformed_metadata_fails_closed_without_partial_fields(self):
        self._persist(self._report(result="UNKNOWN", mappings=[{
            "field_id": "region", "question": "State or province", "type": "select",
            "required": True, "visible": True, "depends_on": "missing-parent",
            "options": ["Oklahoma"], "repeatable": False,
        }]))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual(response.json()["fields"], [])

    def test_stale_field_fingerprint_returns_conflict(self):
        self._persist(self._report())

        response = self.client.get(self.url, params={"expected_fingerprint": "f" * 64})

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")
        self.assertNotIn("fields", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
