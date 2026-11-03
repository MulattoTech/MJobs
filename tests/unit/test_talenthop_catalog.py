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

    def test_original_denominators_cannot_be_redirected_to_new_catalog(self):
        self.data["legacy_acceptance"]["original"] = "docs/talenthop/catalog.json"
        self.assertIn("ACCEPTANCE_AUTHORITY_CHANGED", validate_catalog(self.data, ROOT))


if __name__ == "__main__":
    unittest.main()
