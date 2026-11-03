import copy
import json
from pathlib import Path
import unittest

from tools.verification.talenthop_catalog import validate_catalog

ROOT = Path(__file__).resolve().parents[2]


class TalentHopCatalogTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "docs/talenthop/catalog.json").read_text(encoding="utf-8"))

    def test_current_catalog_is_valid_and_acceptance_files_are_unchanged(self):
        paths = [ROOT / "planning/CAPABILITY_MATRIX.json", ROOT / "planning/REFERENCE_GAP_MATRIX.json"]
        before = [path.read_bytes() for path in paths]
        self.assertEqual(validate_catalog(self.data, ROOT), [])
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_closed_issue_does_not_establish_delivery_evidence(self):
        for state in ("VERIFIED IN TEST", "MERGED", "DEPLOYED", "ACCEPTED"):
            with self.subTest(state=state):
                data = copy.deepcopy(self.data)
                data["features"][0]["implementation_state"] = state
                data["features"][0]["issue_state"] = "closed"
                self.assertIn("FEATURE_DELIVERY_EVIDENCE_MISSING", validate_catalog(data, ROOT))

    def test_duplicate_ids_cycles_invalid_sources_and_unknown_states_fail(self):
        mutations = [
            (lambda d: d["features"][1].update(id=d["features"][0]["id"]), "FEATURE_IDS_INVALID"),
            (lambda d: d["features"][0].update(dependencies=[d["features"][0]["id"]]), "FEATURE_DEPENDENCY_CYCLE"),
            (lambda d: d["features"][0].update(source_paths=["../private.har"]), "FEATURE_SOURCE_PATH_INVALID"),
            (lambda d: d["features"][0].update(discovery_state="DONE"), "FEATURE_STATE_INVALID"),
            (lambda d: d["features"][0].update(evidence=["unretrieved"]), "FEATURE_EVIDENCE_INVALID"),
        ]
        for mutate, expected in mutations:
            with self.subTest(expected=expected):
                data = copy.deepcopy(self.data)
                mutate(data)
                self.assertIn(expected, validate_catalog(data, ROOT))

    def test_coverage_and_har_cannot_be_inflated_without_evidence(self):
        self.data["audit"]["coverage"]["routes_exercised"] = 1000
        self.data["audit"]["har"]["status"] = "FULL"
        errors = validate_catalog(self.data, ROOT)
        self.assertIn("COVERAGE_INVALID", errors)
        self.assertIn("HAR_EVIDENCE_MISSING", errors)

    def test_marketing_evidence_does_not_establish_observed_ui(self):
        row = self.data["features"][0]
        row["discovery_state"] = "OBSERVED"
        row["evidence"] = ["TH-PUBLIC-001"]
        errors = validate_catalog(self.data, ROOT)
        self.assertIn("FEATURE_OBSERVATION_EVIDENCE_MISSING", errors)
        self.assertIn("JOURNEY_EVIDENCE_MISSING", errors)

    def test_exercised_counts_must_match_records_not_just_fit_denominators(self):
        for group, expected in (
            ("routes", "ROUTE_EXERCISED_COUNT_MISMATCH"),
            ("journeys", "JOURNEY_EXERCISED_COUNT_MISMATCH"),
        ):
            with self.subTest(group=group):
                data = copy.deepcopy(self.data)
                data["audit"]["coverage"][group + "_exercised"] = 0
                self.assertIn(expected, validate_catalog(data, ROOT))

    def test_observed_evidence_requires_a_real_bounded_record(self):
        for artifact in ("docs/evidence/nonexistent-audit.json", "../private.json",
                         "docs/evidence/../../private.json", "docs\\\\evidence\\\\private.json"):
            with self.subTest(artifact=artifact):
                data = copy.deepcopy(self.data)
                for item in data["evidence"]:
                    if item.get("kind") == "INTERACTIVE_UI":
                        item["artifact"] = artifact
                errors = validate_catalog(data, ROOT)
                self.assertIn("UI_EVIDENCE_ARTIFACT_INVALID", errors)
                self.assertIn("FEATURE_OBSERVATION_EVIDENCE_MISSING", errors)

    def test_missing_record_wrong_timestamp_and_duplicate_evidence_fail(self):
        ui = next(item for item in self.data["evidence"] if item.get("kind") == "INTERACTIVE_UI")
        data = copy.deepcopy(self.data)
        target = next(item for item in data["evidence"] if item["id"] == ui["id"])
        target["id"] = "TH-NOT-ACTUALLY-RECORDED"
        self.assertIn("UI_EVIDENCE_RECORD_INVALID", validate_catalog(data, ROOT))
        data = copy.deepcopy(self.data)
        target = next(item for item in data["evidence"] if item["id"] == ui["id"])
        target["retrieved_at"] = "2026-01-01T00:00:00Z"
        self.assertIn("UI_EVIDENCE_RECORD_INVALID", validate_catalog(data, ROOT))
        data = copy.deepcopy(self.data)
        data["evidence"].append(copy.deepcopy(data["evidence"][0]))
        self.assertIn("EVIDENCE_IDS_INVALID", validate_catalog(data, ROOT))

    def test_route_observation_cannot_use_a_marketing_claim(self):
        self.data["routes"][0]["evidence"] = "TH-PUBLIC-001"
        self.assertIn("ROUTE_OBSERVATION_EVIDENCE_MISSING", validate_catalog(self.data, ROOT))

    def test_journey_and_route_records_require_boolean_states_and_scopes(self):
        self.data["features"][0]["journey"] = {"exercised": 1, "scope": ""}
        self.data["routes"][0]["exercised"] = 1
        errors = validate_catalog(self.data, ROOT)
        self.assertIn("JOURNEY_SCOPE_INVALID", errors)
        self.assertIn("ROUTE_STATE_INVALID", errors)

    def test_catalog_timestamp_matches_deployed_fleet_contract(self):
        for stamp in ("2026-11-03T08:00:00.123456Z", "2026-11-03T08:00:00+00:00",
                      "2026-02-30T08:00:00Z", "not-a-time", None):
            with self.subTest(stamp=stamp):
                data = copy.deepcopy(self.data)
                data["as_of"] = stamp
                self.assertIn("CATALOG_TIMESTAMP_INVALID", validate_catalog(data, ROOT))

    def test_new_blocker_codes_require_coordinated_consumer_support(self):
        self.data["audit"]["blockers"].append("GENERATION_EXPORT_DELIVERY_NOT_RUN")
        self.assertIn("AUDIT_BLOCKERS_INVALID", validate_catalog(self.data, ROOT))

    def test_original_denominators_cannot_be_redirected_to_new_catalog(self):
        self.data["legacy_acceptance"]["original"] = "docs/talenthop/catalog.json"
        self.assertIn("ACCEPTANCE_AUTHORITY_CHANGED", validate_catalog(self.data, ROOT))


if __name__ == "__main__":
    unittest.main()
