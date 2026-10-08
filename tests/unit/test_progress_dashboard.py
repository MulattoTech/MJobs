"""Tracker snapshots must be complete, deterministic, and written only on demand."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.verification.progress_dashboard import main, render_dashboard, snapshot, write_dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "planning").mkdir()
        (self.root / "docs").mkdir()
        self.seeds = [
            {"id": "MJ-REQ-000", "domain": "governance_safety", "priority": "P0"},
            {"id": "MJ-REQ-001", "domain": "company_ats_discovery", "priority": "P1"},
            {"id": "MJ-REQ-002", "domain": "company_ats_discovery", "priority": "P1"},
        ]
        self.original = {
            "timestamp": "2026-10-08T07:37:22Z",
            "requirements": [
                {"requirement_id": "MJ-REQ-000", "result": "PASS"},
                {"requirement_id": "MJ-REQ-001", "result": "PARTIAL"},
                {"requirement_id": "MJ-REQ-002", "result": "NOT_RUN"},
            ],
        }
        self.reference = {
            "generated_at": "2026-10-07T18:01:27Z",
            "original_catalogue_count": 3,
            "canonical_ids": ["MJ-REF-001", "MJ-REF-002"],
            "reference_gap_count": 2,
            "result_by_id": {
                "MJ-REF-001": {"result": "PASS", "evidence": ["example"]},
                "MJ-REF-002": {"result": "NOT_RUN", "evidence": []},
            },
        }
        self.state = {
            "release_readiness": {"result": "NOT_RUN"},
            "current_milestone": "SOURCE_EXAMPLE",
        }
        self.save()

    def save(self):
        planning = self.root / "planning"
        (planning / "ISSUE_SEED.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in self.seeds), encoding="utf-8"
        )
        for name, item in (
            ("CAPABILITY_MATRIX.json", self.original),
            ("REFERENCE_GAP_MATRIX.json", self.reference),
            ("IMPLEMENTATION_STATE.json", self.state),
        ):
            (planning / name).write_text(json.dumps(item), encoding="utf-8")

    def test_counts_are_derived_from_authoritative_id_sets(self):
        result = snapshot(self.root)
        self.assertEqual(result["original_total"], 3)
        self.assertEqual(result["original"]["PASS"], 1)
        self.assertEqual(result["original"]["PARTIAL"], 1)
        self.assertEqual(result["original"]["NOT_RUN"], 1)
        self.assertEqual(result["reference"]["PASS"], 1)
        self.assertEqual(result["areas"]["company_ats_discovery"]["PARTIAL"], 1)
        self.assertEqual(result["priorities"]["P0"]["PASS"], 1)
        page = render_dashboard(self.root)
        self.assertIn("1 PASS / 1 PARTIAL / 1 NOT_RUN", page)
        self.assertIn("1 PASS / 0 PARTIAL / 1 NOT_RUN", page)
        self.assertIn("Full-spec release readiness: **NOT_RUN**", page)
        self.assertIn("| P0 |", page.replace(chr(96), ""))

    def test_check_is_read_only_and_write_is_idempotent(self):
        target = self.root / "docs/PROGRESS_DASHBOARD.md"
        self.assertEqual(main(["--check", "--root", str(self.root)]), 1)
        self.assertFalse(target.exists())
        self.assertTrue(write_dashboard(self.root))
        before = target.read_bytes()
        initial_mtime = target.stat().st_mtime_ns
        self.assertFalse(write_dashboard(self.root))
        self.assertEqual(target.stat().st_mtime_ns, initial_mtime)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(main(["--check", "--root", str(self.root)]), 0)
        self.original["requirements"][2]["result"] = "PARTIAL"
        self.save()
        self.assertEqual(main(["--check", "--root", str(self.root)]), 1)
        self.assertTrue(write_dashboard(self.root))
        self.assertNotEqual(target.read_bytes(), before)

    def test_rejects_missing_and_duplicate_original_ids(self):
        self.original["requirements"].pop()
        self.save()
        with self.assertRaisesRegex(ValueError, "MJ-REQ ID mismatch"):
            snapshot(self.root)
        self.original["requirements"].append(self.original["requirements"][0])
        self.save()
        with self.assertRaisesRegex(ValueError, "Duplicate MJ-REQ"):
            snapshot(self.root)

    def test_refuses_invalid_reference_count_or_unrecognized_status(self):
        self.reference["reference_gap_count"] = 3
        self.save()
        with self.assertRaisesRegex(ValueError, "MJ-REF"):
            snapshot(self.root)
        self.reference["reference_gap_count"] = 2
        self.original["requirements"][2]["result"] = "FAKE_PASS"
        self.save()
        with self.assertRaisesRegex(ValueError, "Unexpected acceptance"):
            snapshot(self.root)


if __name__ == "__main__":
    unittest.main()
