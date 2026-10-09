"""Value-free dynamic field persistence contract for application preflight."""
from contextlib import contextmanager
import unittest

from services.applications.preflight import build_dynamic_form_snapshot, persist_preflight


def dynamic_report(**overrides):
    report = {
        "result": "UNKNOWN",
        "reason": "DYNAMIC_REQUIRED_FIELDS_REVIEW_REQUIRED",
        "observed_at": "2026-10-09T00:00:00Z",
        "profile_version": "profile-v4",
        "document_version": 3,
        "route_id": "route-a",
        "provider": "mock-ats",
        "tenant": "example",
        "requisition": "B",
        "metadata_fingerprint": "a" * 64,
        "mappings": [
            {
                "question_id": "country", "field_id": "country", "question": "Country",
                "type": "select", "type_explicit": True, "required": True,
                "visible": True, "optional": False, "conditional": False,
                "depends_on": None, "options": ["United States", "Canada"],
                "repeatable": False, "optional_sections": [], "supported": True,
                "metadata_provider": "mock-ats", "metadata_tenant": "example",
                "metadata_requisition": "B", "metadata_revision": 4,
                "fact_ids": [], "answer_ids": ["country-answer"], "profile_paths": [],
                "document_kinds": [], "application_context": [],
                "current_value": "PRIVATE_ANSWER", "api_token": "PRIVATE_TOKEN",
            },
            {
                "question_id": "region", "field_id": "region", "question": "State",
                "type": "select", "type_explicit": True, "required": True,
                "visible": True, "optional": False, "conditional": True,
                "depends_on": "country", "options": ["Oklahoma", "Texas"],
                "repeatable": False, "optional_sections": ["location-details"],
                "supported": False, "metadata_provider": "mock-ats",
                "metadata_tenant": "example", "metadata_requisition": "B",
                "metadata_revision": 4, "fact_ids": [], "answer_ids": [],
                "profile_paths": [], "document_kinds": [], "application_context": [],
            },
            {
                "question_id": "job-title", "field_id": "job-title", "question": "Role title",
                "type": "text", "type_explicit": True, "required": False,
                "visible": True, "optional": True, "conditional": False,
                "depends_on": None, "options": [], "repeatable": True,
                "optional_sections": ["employment-history"], "supported": False,
                "metadata_provider": "mock-ats", "metadata_tenant": "example",
                "metadata_requisition": "B", "metadata_revision": 4,
                "repeatable_group": "employment", "repeatable_min_rows": 1,
                "repeatable_max_rows": 5, "repeatable_group_required": True,
                "repeatable_group_visible": True,
                "fact_ids": [], "answer_ids": [], "profile_paths": [],
                "document_kinds": [], "application_context": [],
            },
        ],
        "missing": [],
    }
    report.update(overrides)
    return report


class _FakeEngine:
    @contextmanager
    def begin(self):
        yield object()


class _CaptureStore:
    def __init__(self):
        self.engine = _FakeEngine()
        self.payload = None

    def _audit(self, _connection, _application_id, _action, payload):
        self.payload = payload


class ApplicationPreflightDynamicMetadataTests(unittest.TestCase):
    def test_snapshot_preserves_safe_sections_groups_dependencies_and_fingerprint(self):
        snapshot = build_dynamic_form_snapshot(dynamic_report())

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["fingerprint"], "a" * 64)
        self.assertEqual(snapshot["optional_sections"], [
            {"section_id": "employment-history", "field_ids": ["job-title"]},
            {"section_id": "location-details", "field_ids": ["region"]},
        ])
        self.assertEqual(snapshot["repeatable_groups"], [{
            "group_id": "employment", "field_ids": ["job-title"],
            "minimum_rows": 1, "maximum_rows": 5,
            "required": True, "visible": True,
        }])
        self.assertEqual(snapshot["dependent_choices"], [{
            "field_id": "region", "depends_on": "country",
            "options": ["Oklahoma", "Texas"],
        }])

    def test_persistence_projects_only_value_free_allowlisted_metadata(self):
        store = _CaptureStore()
        persist_preflight(store, "application-a", dynamic_report())

        self.assertEqual(store.payload["dynamic_fields_fingerprint"], "a" * 64)
        self.assertEqual(len(store.payload["dynamic_form_metadata"]["snapshot_fingerprint"]), 64)
        self.assertEqual([field["field_id"] for field in store.payload["dynamic_fields"]], [
            "country", "region", "job-title",
        ])
        persisted = store.payload["dynamic_form_metadata"]
        self.assertEqual(persisted["optional_sections"][1], {
            "section_id": "location-details", "field_ids": ["region"],
        })
        self.assertEqual(persisted["repeatable_groups"][0]["group_id"], "employment")
        self.assertEqual(persisted["dependent_choices"][0]["depends_on"], "country")
        serialized = str(store.payload)
        self.assertNotIn("PRIVATE_ANSWER", serialized)
        self.assertNotIn("PRIVATE_TOKEN", serialized)
        self.assertNotIn("current_value", serialized)
        self.assertNotIn("api_token", serialized)
        self.assertEqual(store.payload["dynamic_form_metadata"]["fingerprint"], "a" * 64)

    def test_snapshot_fingerprint_changes_with_section_group_or_choice_metadata(self):
        base = build_dynamic_form_snapshot(dynamic_report())
        changed_choice = dynamic_report()
        changed_choice["mappings"][1]["options"] = ["Oklahoma", "Kansas"]
        changed_membership = dynamic_report()
        changed_membership["mappings"][1]["optional_sections"] = ["location-info"]

        self.assertNotEqual(base["snapshot_fingerprint"],
                            build_dynamic_form_snapshot(changed_choice)["snapshot_fingerprint"])
        self.assertNotEqual(base["snapshot_fingerprint"],
                            build_dynamic_form_snapshot(changed_membership)["snapshot_fingerprint"])

    def test_malformed_metadata_is_not_persisted_as_dynamic_snapshot(self):
        store = _CaptureStore()
        report = dynamic_report()
        report["mappings"][1]["depends_on"] = "missing-parent"
        persist_preflight(store, "application-a", report)

        self.assertNotIn("dynamic_form_metadata", store.payload)
        self.assertNotIn("dynamic_fields", store.payload)
        self.assertNotIn("PRIVATE_ANSWER", str(store.payload))
        self.assertNotIn("PRIVATE_TOKEN", str(store.payload))

    def test_unbounded_metadata_is_dropped_instead_of_audited(self):
        store = _CaptureStore()
        report = dynamic_report()
        report["mappings"][0]["options"] = [f"choice-{index}" for index in range(101)]

        persist_preflight(store, "application-a", report)

        self.assertNotIn("dynamic_form_metadata", store.payload)
        self.assertNotIn("dynamic_fields", store.payload)
        self.assertNotIn("options", str(store.payload))

    def test_cyclic_dependent_choice_metadata_is_rejected(self):
        report = dynamic_report()
        report["mappings"][0]["depends_on"] = "region"

        self.assertIsNone(build_dynamic_form_snapshot(report))


if __name__ == "__main__":
    unittest.main()
