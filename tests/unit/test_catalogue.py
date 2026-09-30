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
            canonical = [f"MJ-REF-{number:03d}" for number in range(1, 43)]
            (root / "planning/REFERENCE_GAP_ISSUE_MAP.json").write_text(
                json.dumps({"issues": {identifier: number for number, identifier in enumerate(canonical, 371)}}),
                encoding="utf-8")
            (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(
                json.dumps({"canonical_ids": canonical, "reference_gap_count": 42,
                            "original_catalogue_count": 1, "result_by_id": {}}), encoding="utf-8")
            self.assertIn("Reference completion statuses must cover all 42 canonical issues",
                          validate_catalogue(root))
            statuses = {identifier: {"result": "NOT_RUN", "evidence": []} for identifier in canonical}
            statuses[canonical[0]]["result"] = "PASS"
            (root / "planning/REFERENCE_GAP_MATRIX.json").write_text(
                json.dumps({"canonical_ids": canonical, "reference_gap_count": 42,
                            "original_catalogue_count": 1, "result_by_id": statuses}), encoding="utf-8")
            self.assertIn(f"Reference PASS requires evidence: {canonical[0]}", validate_catalogue(root))


if __name__ == "__main__":
    unittest.main()
