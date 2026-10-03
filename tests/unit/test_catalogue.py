"""Catch lost requirements, invalid dependencies, and ambiguous issue markers."""
import json
from pathlib import Path
import unittest

from tools.verification.catalogue import validate_catalogue

ROOT = Path(__file__).resolve().parents[2]


class CatalogueTests(unittest.TestCase):
    def test_real_catalogue_covers_every_requirement(self):
        self.assertEqual(validate_catalogue(ROOT), [])

    def test_validator_catches_missing_requirement_and_cycle(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "planning").mkdir()
            (root / "planning/ISSUE_SEED.jsonl").write_text(
                json.dumps({"id": "A", "body": "<!-- mj-requirement:A -->"}) + "\n" +
                json.dumps({"id": "B", "body": "<!-- mj-requirement:B -->"}) + "\n",
                encoding="utf-8")
            (root / "planning/PR_QUEUE.json").write_text(json.dumps({"items": [
                {"id": "P1", "requirements": ["A"], "depends_on": ["P2"]},
                {"id": "P2", "requirements": [], "depends_on": ["P1"]},
            ]}), encoding="utf-8")
            errors = validate_catalogue(root)
            self.assertIn("Unqueued requirement: B", errors)
            self.assertIn("Dependency cycle: P1", errors)

    def test_validator_catches_incomplete_reference_statuses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "planning").mkdir()
            (root / "planning/ISSUE_SEED.jsonl").write_text(
                json.dumps({"id": "A", "body": "<!-- mj-requirement:A -->"}) + "\n", encoding="utf-8")
            (root / "planning/PR_QUEUE.json").write_text(
                json.dumps({"items": [{"id": "P1", "requirements": ["A"]}]}), encoding="utf-8")
            canonical = [f"MJ-REF-{number:03d}" for number in range(1, 60)]
            (root / "planning/REFERENCE_GAP_ISSUE_MAP.json").write_text(
                json.dumps({"issues": {identifier: number for number, identifier in enumerate(canonical, 371)}}),
                encoding="utf-8")
            (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(
                json.dumps({"canonical_ids": canonical, "reference_gap_count": 59,
                            "original_catalogue_count": 1, "result_by_id": {}}), encoding="utf-8")
            self.assertIn("Reference completion statuses must cover all 59 canonical issues",
                          validate_catalogue(root))
            statuses = {identifier: {"result": "NOT_RUN", "evidence": []} for identifier in canonical}
            statuses[canonical[0]]["result"] = "PASS"
            (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(
                json.dumps({"canonical_ids": canonical, "reference_gap_count": 59,
                            "original_catalogue_count": 1, "result_by_id": statuses}), encoding="utf-8")
            self.assertIn(f"Reference PASS requires evidence: {canonical[0]}", validate_catalogue(root))

    def test_validator_requires_each_commercial_feature_record(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        del matrix["commercial_features"]["MJ-REF-043"]
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        self.assertIn("Commercial feature record missing: MJ-REF-043", validate_catalogue(root))

    def test_validator_rejects_bad_commercial_prerequisite(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        matrix["commercial_features"]["MJ-REF-043"]["prerequisites"] = ["MJ-REQ-999"]
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        self.assertIn("Invalid commercial prerequisite: MJ-REF-043 -> MJ-REQ-999", validate_catalogue(root))

    def test_validator_rejects_commercial_prerequisite_cycle(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        matrix["commercial_features"]["MJ-REF-043"]["prerequisites"] = ["MJ-REF-044"]
        matrix["commercial_features"]["MJ-REF-044"]["prerequisites"] = ["MJ-REF-043"]
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        self.assertIn("Commercial prerequisite cycle: MJ-REF-043", validate_catalogue(root))

    def test_validator_allows_explicit_mapped_issue_and_requires_equivalence_note(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        matrix["commercial_features"]["MJ-REF-044"].update(
            issue_action="MAP_EXISTING", equivalence_note="Shares the existing issue contract.")
        issue_map = json.loads((root / "planning/REFERENCE_GAP_ISSUE_MAP.json").read_text())
        issue_map["issues"]["MJ-REF-044"] = issue_map["issues"]["MJ-REF-043"]
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        (root / "planning/REFERENCE_GAP_ISSUE_MAP.json").write_text(json.dumps(issue_map))
        self.assertNotIn("Unexplained duplicate reference issue", "\n".join(validate_catalogue(root)))

    def test_validator_does_not_infer_pass_from_feature_presence(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        matrix["result_by_id"]["MJ-REF-043"] = {"result": "NOT_RUN", "evidence": []}
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        self.assertEqual(validate_catalogue(root), [])

    def test_validator_blocks_commercial_pass_until_original_prerequisites_pass(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        matrix["result_by_id"]["MJ-REF-043"] = {
            "result": "PASS", "evidence": ["commercial-evidence.json"]
        }
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        errors = validate_catalogue(root)
        self.assertIn(
            "Commercial prerequisite not PASS: MJ-REF-043 -> MJ-REQ-001", errors
        )

    def test_validator_reports_malformed_top_level_status_containers(self):
        for field, expected in (
            ("issues", "Reference issue map issues must be an object"),
            ("result_by_id", "Reference completion statuses must be an object"),
        ):
            root = self._commercial_fixture()
            path = root / "planning" / (
                "REFERENCE_GAP_ISSUE_MAP.json"
                if field == "issues" else "REFERENCE_GAP_MATRIX.json"
            )
            payload = json.loads(path.read_text())
            payload[field] = None
            path.write_text(json.dumps(payload))
            errors = validate_catalogue(root)
            self.assertIn(expected, errors)

    def test_validator_reports_malformed_status_record_without_crashing(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        matrix["result_by_id"]["MJ-REF-043"] = None
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        errors = validate_catalogue(root)
        self.assertIn("Invalid reference completion record: MJ-REF-043", errors)

    def test_validator_reports_nonlist_commercial_prerequisites_without_crashing(self):
        for malformed in (None, {"id": "MJ-REF-044"}):
            root = self._commercial_fixture()
            matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
            matrix["commercial_features"]["MJ-REF-043"]["prerequisites"] = malformed
            (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
            errors = validate_catalogue(root)
            self.assertIn("Commercial checklist must be a list: MJ-REF-043.prerequisites", errors)

    def test_validator_reports_unhashable_commercial_references_without_crashing(self):
        root = self._commercial_fixture()
        matrix = json.loads((root / "planning/REFERENCE_GAP_MATRIX.json").read_text())
        matrix["commercial_features"]["MJ-REF-043"]["prerequisites"] = [["MJ-REF-044"]]
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps(matrix))
        errors = validate_catalogue(root)
        self.assertIn("Commercial references must contain strings: MJ-REF-043.prerequisites", errors)

    def test_validator_reports_unhashable_issue_map_value_without_crashing(self):
        root = self._commercial_fixture()
        issue_map = json.loads((root / "planning/REFERENCE_GAP_ISSUE_MAP.json").read_text())
        issue_map["issues"]["MJ-REF-043"] = [403]
        (root / "planning/REFERENCE_GAP_ISSUE_MAP.json").write_text(json.dumps(issue_map))
        errors = validate_catalogue(root)
        self.assertIn("Reference issue map must contain positive integer issues for all 59 canonical issues", errors)

    def _commercial_fixture(self):
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "planning").mkdir()
        (root / "docs/spec/areas").mkdir(parents=True)
        (root / "docs/spec/areas/commercial.md").write_text("area")
        seeds = [{"id": "MJ-REQ-001", "body": "<!-- mj-requirement:MJ-REQ-001 -->"}]
        (root / "planning/ISSUE_SEED.jsonl").write_text("\n".join(json.dumps(seed) for seed in seeds) + "\n")
        queue_ids = [f"MJ-PR-{number:03d}" for number in range(1, 4)]
        (root / "planning/PR_QUEUE.json").write_text(json.dumps({"items": [
            {"id": identifier, "requirements": ["MJ-REQ-001"] if number == 1 else [], "depends_on": []}
            for number, identifier in enumerate(queue_ids, 1)
        ]}))
        canonical = [f"MJ-REF-{number:03d}" for number in range(1, 60)]
        issues = {identifier: 370 + number for number, identifier in enumerate(canonical)}
        features = {
            identifier: {
                "title": identifier, "problem": "problem", "scope_distinction": "distinct",
                "owner_area": "commercial", "priority": "P4", "original_overlaps": ["MJ-REQ-001"],
                "reference_overlaps": [], "prerequisites": ["MJ-REQ-001"], "queue_prerequisites": ["MJ-PR-001"],
                "acceptance_criteria": ["criterion"], "test_strategy": ["test"],
                "issue_action": "CREATE_NEW", "equivalence_note": "", "inspiration": ["reference"],
            } for identifier in canonical if int(identifier[-3:]) >= 43
        }
        statuses = {identifier: {"result": "NOT_RUN", "evidence": []} for identifier in canonical}
        (root / "planning/REFERENCE_GAP_ISSUE_MAP.json").write_text(json.dumps({"issues": issues}))
        (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(json.dumps({
            "canonical_ids": canonical, "reference_gap_count": 59, "original_catalogue_count": 1,
            "result_by_id": statuses, "commercial_features": features,
        }))
        return root


if __name__ == "__main__":
    unittest.main()
