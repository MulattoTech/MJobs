"""Unit checks for the Windows updater; no Docker or GitHub traffic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from infra.deploy.windows import update_mjobs_micro as cd


def check(name, *, ident=1, status="completed", conclusion="success", app="github-actions"):
    return {"name": name, "id": ident, "status": status, "conclusion": conclusion,
            "app": {"slug": app}, "started_at": "2026-10-24T00:00:00Z"}


class WindowsCDTests(unittest.TestCase):
    def test_all_verified_exact_named_gates_required(self):
        rows = [check(name, ident=i) for i, name in enumerate(cd.REQUIRED)]
        with patch.object(cd, "command", return_value=json.dumps(
                {"check_runs": rows, "total_count": len(rows)})):
            self.assertTrue(cd.checks_pass("a" * 40))

    def test_missing_pending_skipped_or_fake_app_rejected(self):
        cases = [[], [check(cd.REQUIRED[0], status="in_progress")],
                 [check(cd.REQUIRED[0], conclusion="skipped")],
                 [check(cd.REQUIRED[0], app="unknown")]]
        for rows in cases:
            with self.subTest(rows=rows), patch.object(cd, "command", return_value=json.dumps(
                    {"check_runs": rows, "total_count": len(rows)})):
                self.assertFalse(cd.checks_pass("a" * 40))

    def test_newer_failed_check_overrides_earlier_green(self):
        rows = [check(name, ident=i) for i, name in enumerate(cd.REQUIRED)]
        rows.append(check(cd.REQUIRED[0], ident=100, conclusion="failure"))
        with patch.object(cd, "command", return_value=json.dumps(
                {"check_runs": rows, "total_count": len(rows)})):
            self.assertFalse(cd.checks_pass("a" * 40))

    def test_ambiguous_check_page_blocks(self):
        with patch.object(cd, "command", return_value=json.dumps(
                {"check_runs": [], "total_count": 101})):
            with self.assertRaises(RuntimeError):
                cd.checks_pass("a" * 40)

    def test_migration_revision_change_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "migrations").mkdir()
            (root / "migrations" / "a.py").write_text("revision='1'\n", encoding="utf-8")
            (root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
            before = cd.migration_digest(root)
            (root / "migrations" / "a.py").write_text("revision='2'\n", encoding="utf-8")
            self.assertNotEqual(before, cd.migration_digest(root))

    def test_container_mode_is_personal_and_public_host_is_explicit(self):
        with patch.object(cd, "command") as executor:
            cd.compose(Path("foo"), "b" * 40, "mjobs-micro:sha-" + "b" * 40, "ps")
            args, kwargs = executor.call_args
            self.assertEqual(kwargs["env"]["MJOBS_COMPOSE_MOCK"], "0")
            self.assertEqual(kwargs["env"]["MJOBS_OPERATION_MODE"], "PERSONAL")
            self.assertEqual(kwargs["env"]["MJOBS_PUBLIC_HOST"], cd.PUBLIC_HOST)
            self.assertEqual(kwargs["env"]["MJOBS_PORT"], "18801")
            self.assertEqual(args[0][-1], "ps")


if __name__ == "__main__":
    unittest.main()
