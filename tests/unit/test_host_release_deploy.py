"""Unit contracts for the root-owned release reconciler: no network/Docker writes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from infra.deploy import host_update as cd


class HostReleaseTests(unittest.TestCase):
    def test_approved_current_checks_must_all_be_real_and_successful(self):
        rows = [
            {"name": n, "app": {"slug": "github-actions"},
             "status": "completed", "conclusion": "success", "id": i}
            for i, n in enumerate(cd.CHECKS)
        ]
        with patch.object(cd, "https_json", return_value={"total_count": len(rows), "check_runs": rows}):
            self.assertEqual(set(cd.current_checks("b" * 40).values()), {"SUCCESS"})

    def test_old_green_does_not_override_newer_failure(self):
        name = cd.CHECKS[0]
        rows = [{"name": name, "app": {"slug": "github-actions"},
                 "status": "completed", "conclusion": outcome, "id": i}
                for i, outcome in [(5, "success"), (12, "failure")]]
        with patch.object(cd, "https_json", return_value={"total_count": 2, "check_runs": rows}):
            statuses = cd.current_checks("c" * 40)
        self.assertEqual(statuses[name], "FAILURE")
        for absent in cd.CHECKS[1:]:
            self.assertEqual(statuses[absent], "MISSING")

    def test_unrelated_app_is_ignored(self):
        rows = [{"name": cd.CHECKS[0], "app": {"slug": "malicious"},
                 "status": "completed", "conclusion": "success", "id": 22}]
        with patch.object(cd, "https_json", return_value={"total_count": 1, "check_runs": rows}):
            self.assertEqual(cd.current_checks("d" * 40)[cd.CHECKS[0]], "MISSING")

    def test_ambiguous_check_pagination_fails_closed(self):
        with patch.object(cd, "https_json", return_value={"total_count": 101, "check_runs": []}):
            with self.assertRaises(cd.DeployError):
                cd.current_checks("e" * 40)

    def test_migration_source_fingerprint_blocks_any_schema_change(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            migrations = root / "migrations"
            migrations.mkdir()
            (migrations / "revision.py").write_text("revision = 'alpha'\n")
            (root / "alembic.ini").write_text("[alembic]\n")
            before = cd.fingerprint(root)
            (migrations / "revision.py").write_text("revision = 'beta'\n")
            self.assertNotEqual(cd.fingerprint(root), before)

    def test_runtime_options_do_not_enable_fixture_or_external_submissions(self):
        env = cd.environment("a" * 40, "mjobs-local:sha-" + "a" * 40)
        self.assertEqual(env["MJOBS_OPERATION_MODE"], "PERSONAL")
        self.assertEqual(env["MJOBS_PUBLIC_HOST"], cd.PUBLIC_HOST)
        self.assertEqual(env["MJOBS_COMPOSE_MOCK"], "0")
        self.assertEqual(env["MJOBS_DEPLOY_SHA"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
