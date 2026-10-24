"""Deterministic deployment gate regression tests; no network access."""
import unittest

from tools.deploy.wait_for_checks import MAIN_GATES, assess_checks


def check(name, conclusion="success", *, id=1, status="completed", app="github-actions"):
    return {"name": name, "conclusion": conclusion, "status": status,
            "started_at": "2026-10-24T00:00:00Z", "id": id, "app": {"slug": app}}


class DeploymentCheckTests(unittest.TestCase):
    def test_exact_success_required_for_all_independent_gates(self):
        rows = [check(name, id=i) for i, name in enumerate(MAIN_GATES)]
        self.assertEqual(assess_checks(rows), {name: "SUCCESS" for name in MAIN_GATES})

    def test_missing_pending_and_skipped_are_not_success(self):
        self.assertEqual(assess_checks([]), {name: "MISSING" for name in MAIN_GATES})
        rows = [check(MAIN_GATES[0], status="in_progress"),
                check(MAIN_GATES[1], "skipped")]
        state = assess_checks(rows)
        self.assertEqual(state[MAIN_GATES[0]], "PENDING")
        self.assertEqual(state[MAIN_GATES[1]], "SKIPPED")
        self.assertEqual(state[MAIN_GATES[2]], "MISSING")

    def test_newer_failure_cannot_be_hidden_by_old_success(self):
        rows = [check(MAIN_GATES[0], id=10),
                check(MAIN_GATES[0], conclusion="failure", id=20)]
        self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "FAILURE")

    def test_untrusted_app_cannot_masquerade_as_required_check(self):
        rows = [check(MAIN_GATES[0], app="unknown")]
        self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "MISSING")

    def test_other_successful_workflow_is_not_a_substitute(self):
        rows = [check("Web / Vitest and production build")]
        self.assertEqual(assess_checks(rows)[MAIN_GATES[0]], "MISSING")


if __name__ == "__main__":
    unittest.main()
