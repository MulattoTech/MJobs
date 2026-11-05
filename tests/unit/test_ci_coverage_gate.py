"""Synthetic coverage-gate controls; no live provider or private data."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.ci.coverage_gate import CoverageGateError, main, measure


POLICY = {
    "schema_version": 1,
    "backend": {"files": ["services/ai/tasks.py", "services/ai/prompt_versioning.py"],
                "minimum_lines_pct": 60},
    "frontend": {"files": ["src/ai-provider-settings.tsx"], "minimum_lines_pct": 50},
}
BACKEND = {
    "files": {
        "services/ai/tasks.py": {"summary": {"covered_lines": 6, "num_statements": 10}},
        "services/ai/prompt_versioning.py": {
            "summary": {"covered_lines": 8, "num_statements": 10}},
    },
}
FRONTEND = {
    "total": {"lines": {"covered": 2, "total": 3}},
    "C:/work/apps/web/src/ai-provider-settings.tsx": {
        "lines": {"covered": 8, "total": 10}},
}


class CoverageGateTests(unittest.TestCase):
    def test_backend_exact_scope_and_threshold(self):
        result = measure("backend", BACKEND, POLICY)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["covered_lines"], 14)
        self.assertEqual(result["total_lines"], 20)
        self.assertEqual(result["measured_lines_pct"], 70)
        self.assertEqual(len(result["files"]), 2)

    def test_frontend_absolute_paths_match_expected_suffix(self):
        result = measure("frontend", FRONTEND, POLICY)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["measured_lines_pct"], 80)
        self.assertEqual(result["files"][0]["path"], "src/ai-provider-settings.tsx")

    def test_real_failure_is_not_rounded_up_to_pass(self):
        policy = {**POLICY, "backend": {**POLICY["backend"],
                                       "minimum_lines_pct": 70.01}}
        result = measure("backend", BACKEND, policy)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["measured_lines_pct"], 70)

    def test_missing_file_fails_closed_instead_of_shrinking_denominator(self):
        report = {"files": {"services/ai/tasks.py": BACKEND["files"]["services/ai/tasks.py"]}}
        with self.assertRaisesRegex(CoverageGateError, "MISSING_OR_AMBIGUOUS_SOURCE"):
            measure("backend", report, POLICY)

    def test_ambiguous_file_fails_closed(self):
        report = {"files": {**BACKEND["files"],
                            "other/services/ai/tasks.py": BACKEND["files"]["services/ai/tasks.py"]}}
        with self.assertRaisesRegex(CoverageGateError, "MISSING_OR_AMBIGUOUS_SOURCE"):
            measure("backend", report, POLICY)

    def test_bad_numeric_and_empty_counts_fail_closed(self):
        for covered, total in ((True, 10), (-1, 10), (11, 10), (0, 0), (5.5, 10)):
            report = {"files": {**BACKEND["files"],
                                "services/ai/tasks.py": {
                                    "summary": {"covered_lines": covered,
                                                "num_statements": total}}}}
            with self.subTest(covered=covered, total=total):
                with self.assertRaisesRegex(CoverageGateError, "INVALID_COVERAGE_TOTAL"):
                    measure("backend", report, POLICY)

    def test_vendor_generated_private_and_parent_paths_forbidden(self):
        for path in ("../private/foo.py", "vendor/code.py", "src/generated/a.tsx",
                     "node_modules/a.tsx", "/private/secrets.py", "C:/safe/foo.py"):
            policy = {**POLICY, "backend": {"files": [path],
                                           "minimum_lines_pct": 20}}
            with self.subTest(path=path):
                with self.assertRaisesRegex(CoverageGateError, "UNSAFE_POLICY_SOURCE"):
                    measure("backend", BACKEND, policy)

    def test_machine_readable_receipt_pass_fail_and_error(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            policy_file, report_file, output = (
                root / "policy.json", root / "report.json", root / "receipt.json")
            policy_file.write_text(json.dumps(POLICY), encoding="utf-8")
            report_file.write_text(json.dumps(BACKEND), encoding="utf-8")
            args = ["--kind", "backend", "--report", str(report_file),
                    "--output", str(output), "--policy", str(policy_file)]
            self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(output.read_text())["status"], "PASS")
            policy_file.write_text(json.dumps({
                **POLICY, "backend": {**POLICY["backend"],
                                    "minimum_lines_pct": 90}}), encoding="utf-8")
            self.assertEqual(main(args), 1)
            self.assertEqual(json.loads(output.read_text())["status"], "FAIL")
            report_file.write_text("{invalid", encoding="utf-8")
            self.assertEqual(main(args), 2)
            self.assertEqual(json.loads(output.read_text())["error_code"],
                             "INVALID_JSON_INPUT")


if __name__ == "__main__":
    unittest.main()
