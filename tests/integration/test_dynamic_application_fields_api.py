"""Read-only profile-scoped dynamic field metadata API contract."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.dynamic_application_fields import create_dynamic_application_fields_router
from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class DynamicApplicationFieldsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-dynamic-fields-api-")
        self.root = Path(self.temp.name)
        self.database = self.root / "dynamic-fields.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        self.client = self._client()
        self.url = f"/profiles/fixture/applications/{self.application_id}/dynamic-fields"

    def _client(self):
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")
        app.include_router(create_dynamic_application_fields_router(self.store))
        return TestClient(app)

    def _persist_fields(self, *, profile_version=None, document_version=None,
                        metadata_fingerprint="a" * 64, mappings=None, optional_sections=None):
        application = self.store.application(self.application_id)
        report = {
            "result": "PASS",
            "reason": None,
            "observed_at": "2026-10-09T00:00:00Z",
            "profile_version": profile_version or application["profile_version"],
            "document_version": document_version or application["document_version"],
            "route_id": application["route_id"],
            "provider": "mock-ats",
            "tenant": "example",
            "requisition": "A",
            "metadata_fingerprint": metadata_fingerprint,
            "optional_sections": optional_sections if optional_sections is not None else [],
            "mappings": mappings if mappings is not None else [
                {
                    "question_id": "country",
                    "kind": "select",
                    "label": "Country",
                    "required": True,
                    "visible": True,
                    "dependent_on": None,
                    "choices": ["United States", "Canada"],
                    "repeatable": False,
                },
                {
                    "question_id": "region",
                    "kind": "select",
                    "label": "State or province",
                    "required": True,
                    "visible": True,
                    "dependent_on": "country",
                    "choices": ["Oklahoma", "Texas"],
                    "repeatable": False,
                },
                {
                    "question_id": "experience_employer",
                    "kind": "text",
                    "label": "Employer",
                    "required": False,
                    "visible": True,
                    "dependent_on": None,
                    "choices": [],
                    "repeatable": True,
                },
            ],
        }
        with self.store.engine.begin() as connection:
            self.store._audit(connection, self.application_id,
                              "APPLICATION_QUESTION_PREFLIGHT", report)
        return report

    def _audit_count(self):
        with self.store.engine.connect() as connection:
            rows = connection.execute(
                select(m.audit.c.id).where(
                    m.audit.c.entity_id == self.application_id,
                    m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
                ),
            ).all()
        return len(rows)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_read_returns_safe_field_identity_and_dynamic_shape_without_answers(self):
        self._persist_fields()
        before = self._audit_count()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["application_id"], self.application_id)
        self.assertRegex(body["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(body["fields"], [
            {"id": "country", "label": "Country", "type": "select",
             "required": True, "visible": True, "depends_on": None,
             "options": ["United States", "Canada"], "repeatable": False},
            {"id": "region", "label": "State or province", "type": "select",
             "required": True, "visible": True, "depends_on": "country",
             "options": ["Oklahoma", "Texas"], "repeatable": False},
            {"id": "experience_employer", "label": "Employer", "type": "text",
             "required": False, "visible": True, "depends_on": None,
             "options": [], "repeatable": True},
        ])
        self.assertNotIn("answers", body)
        self.assertNotIn("value", body)
        self.assertEqual(self._audit_count(), before)
        current = self.client.get(
            self.url, params={"expected_fingerprint": body["fingerprint"]},
        )
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json(), body)
        invalid = self.client.get(self.url, params={"expected_fingerprint": "not-a-fingerprint"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"]["code"], "INVALID_EXPECTED_FINGERPRINT")

    def test_foreign_profile_cannot_read_application_field_state(self):
        response = self.client.get(self.url.replace("/profiles/fixture/", "/profiles/other/"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "APPLICATION_NOT_FOUND")

    def test_expected_old_fingerprint_gets_safe_stale_snapshot_readback(self):
        self._persist_fields()
        response = self.client.get(self.url, params={"expected_fingerprint": "b" * 64})
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "DYNAMIC_FIELDS_STALE")
        self.assertNotIn("fields", detail)

    def test_profile_or_document_version_drift_rejects_field_snapshot(self):
        application = self.store.application(self.application_id)
        self._persist_fields(profile_version="stale-profile-version")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")

        self._persist_fields(document_version=application["document_version"] + 1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")

        self._persist_fields()
        self.store.save_profile({**PROFILE, "name": "Updated Synthetic Applicant"})
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")

    def test_unavailable_or_malformed_metadata_fails_closed(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual(response.json()["fields"], [])

        self._persist_fields(mappings=[{
            "question_id": "region", "kind": "select", "required": True,
            "label": "State or province", "visible": True, "dependent_on": "country",
            "choices": ["OK", {"value": "PRIVATE_ANSWER"}],
            "repeatable": False,
        }])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual(response.json()["fields"], [])
        self.assertNotIn("PRIVATE_ANSWER", response.text)

    def test_cyclic_field_dependencies_fail_closed(self):
        self._persist_fields(mappings=[
            {
                "question_id": "country", "kind": "select", "label": "Country",
                "required": True, "visible": True, "dependent_on": "region",
                "choices": ["United States"], "repeatable": False,
            },
            {
                "question_id": "region", "kind": "select", "label": "Region",
                "required": True, "visible": True, "dependent_on": "country",
                "choices": ["Oklahoma"], "repeatable": False,
            },
        ])

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json().get("status"), "REVIEW_REQUIRED")
        self.assertEqual(response.json().get("fields"), [])

    def test_optional_sections_and_repeatable_rows_are_projected_without_values(self):
        sections = [{
            "section_id": "prior_roles",
            "label": "Additional experience",
            "question_ids": ["role_employer", "role_title"],
            "required": False,
            "visible": True,
            "condition": {"question_id": "include_prior_roles", "equals": "yes"},
        }]
        mappings = [
            {
                "field_id": "role_employer", "question": "Employer", "type": "text",
                "required": True, "visible": True, "depends_on": None,
                "options": [], "repeatable": True, "section_id": "prior_roles",
                "repeatable_group": "prior_roles_rows", "repeatable_min_rows": 0,
                "repeatable_max_rows": 5, "current_value": "PRIVATE_ANSWER",
            },
            {
                "field_id": "role_title", "question": "Title", "type": "text",
                "required": True, "visible": True, "depends_on": None,
                "options": [], "repeatable": True, "section_id": "prior_roles",
                "repeatable_group": "prior_roles_rows", "repeatable_min_rows": 0,
                "repeatable_max_rows": 5,
            },
        ]
        self._persist_fields(mappings=mappings, optional_sections=sections)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body.get("optional_sections"), [{
            "id": "prior_roles", "label": "Additional experience",
            "field_ids": ["role_employer", "role_title"],
            "required": False, "visible": True,
        }])
        self.assertEqual(body.get("repeatable_groups"), [{
            "id": "prior_roles_rows", "field_ids": ["role_employer", "role_title"],
            "minimum_rows": 0, "maximum_rows": 5,
        }])
        self.assertNotIn("PRIVATE_ANSWER", response.text)
        self.assertNotIn("condition", response.text)

    def test_repeatable_bounds_change_invalidates_previous_fingerprint(self):
        mappings = [{
            "field_id": "role_employer", "question": "Employer", "type": "text",
            "required": True, "visible": True, "depends_on": None, "options": [],
            "repeatable": True, "repeatable_group": "prior_roles_rows",
            "repeatable_min_rows": 0, "repeatable_max_rows": 5,
        }]
        self._persist_fields(mappings=mappings)
        first = self.client.get(self.url)
        self.assertEqual(first.status_code, 200, first.text)

        mappings[0]["repeatable_max_rows"] = 6
        self._persist_fields(mappings=mappings)
        stale = self.client.get(
            self.url, params={"expected_fingerprint": first.json()["fingerprint"]},
        )

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "DYNAMIC_FIELDS_STALE")

    def test_review_required_metadata_keeps_safe_fields_visible(self):
        report = self._persist_fields()
        report["result"] = "UNKNOWN"
        report["mappings"][0]["current_value"] = "PRIVATE_ANSWER"
        with self.store.engine.begin() as connection:
            self.store._audit(connection, self.application_id,
                              "APPLICATION_QUESTION_PREFLIGHT", report)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "REVIEW_REQUIRED")
        self.assertEqual([field["id"] for field in body["fields"]], [
            "country", "region", "experience_employer",
        ])
        self.assertNotIn("PRIVATE_ANSWER", response.text)

    def test_optional_section_with_unknown_field_fails_closed(self):
        mappings = [{
            "field_id": "role_employer", "question": "Employer", "type": "text",
            "required": True, "visible": True, "depends_on": None,
            "options": [], "repeatable": False,
        }]
        sections = [{
            "section_id": "prior_roles", "question_ids": ["missing_field"],
        }]
        self._persist_fields(mappings=mappings, optional_sections=sections)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual(response.json()["fields"], [])

    def test_malformed_optional_section_or_repeatable_bounds_fail_closed(self):
        mappings = [{
            "field_id": "role_employer", "question": "Employer", "type": "text",
            "required": True, "visible": True, "depends_on": None, "options": [],
            "repeatable": True, "repeatable_group": "prior_roles_rows",
            "repeatable_min_rows": 3, "repeatable_max_rows": 2,
        }]
        self._persist_fields(mappings=mappings)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json().get("status"), "REVIEW_REQUIRED")
        self.assertEqual(response.json().get("fields"), [])

    def test_field_and_option_bounds_fail_closed_without_partial_metadata(self):
        self._persist_fields(mappings=[{
            "question_id": "region", "kind": "select", "label": "R" * 501,
            "required": True, "visible": True, "dependent_on": "country",
            "choices": ["Oklahoma"], "repeatable": False,
        }])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual(response.json()["fields"], [])

        self._persist_fields(mappings=[{
            "question_id": "region", "kind": "select", "label": "Region",
            "required": True, "visible": True, "dependent_on": "country",
            "choices": [f"option-{index}" for index in range(101)], "repeatable": False,
        }])
        response = self.client.get(self.url)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual(response.json()["fields"], [])

        too_many_fields = [{
            "question_id": f"field-{index}", "kind": "text", "label": "Field",
            "required": False, "visible": True, "dependent_on": None,
            "choices": [], "repeatable": False,
        } for index in range(201)]
        self._persist_fields(mappings=too_many_fields)
        response = self.client.get(self.url)
        self.assertEqual(response.json()["status"], "REVIEW_REQUIRED")
        self.assertEqual(response.json()["fields"], [])

    def test_router_has_no_write_or_external_submission_operations(self):
        router = create_dynamic_application_fields_router(self.store)
        methods = {method for route in router.routes for method in route.methods}
        self.assertEqual(methods, {"GET"})
        self.assertTrue(all("submit" not in route.path.casefold() for route in router.routes))


if __name__ == "__main__":
    unittest.main()
