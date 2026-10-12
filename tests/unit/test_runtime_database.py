"""File-backed runtime credentials fail closed without exposing their content."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class RuntimeDatabaseTests(unittest.TestCase):
    def test_file_credentials_replace_process_url_without_touching_source_file(self):
        from cli.runtime_database import runtime_environment
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "runtime-url"
            content = "postgresql+psycopg://mjobs_runtime:synthetic-only@postgres/mjobs"
            path.write_text(content, encoding="utf-8")
            original = {"MJOBS_DATABASE_URL_FILE": str(path), "OTHER": "preserved"}
            actual = runtime_environment(original)
            self.assertEqual(actual["MJOBS_DATABASE_URL"], content)
            self.assertEqual(actual["OTHER"], "preserved")
            self.assertNotIn("MJOBS_DATABASE_URL", original)
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_missing_invalid_admin_or_ambiguous_file_never_falls_back(self):
        from cli.runtime_database import runtime_environment, RuntimeDatabaseError
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "runtime-url"
            environment = {"MJOBS_DATABASE_URL_FILE": str(path)}
            with self.assertRaises(RuntimeDatabaseError):
                runtime_environment(environment)
            for content in ("", "sqlite:///secret", "postgresql+psycopg://mjobs:secret@postgres/mjobs",
                            "postgresql+psycopg://mjobs_runtime:secret@postgres/mjobs\nextra"):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(RuntimeDatabaseError) as error:
                        runtime_environment(environment)
                    self.assertNotIn("secret", str(error.exception))
            path.write_text("postgresql+psycopg://mjobs_runtime:secret@postgres/mjobs", encoding="utf-8")
            with self.assertRaises(RuntimeDatabaseError):
                runtime_environment(dict(environment, MJOBS_DATABASE_URL="sqlite://"))

    def test_non_compose_commands_preserve_existing_configuration(self):
        from cli.runtime_database import runtime_environment
        original = {"MJOBS_DATABASE_URL": "sqlite://", "OTHER": "value"}
        self.assertEqual(runtime_environment(original), original)

    def test_exec_preserves_argument_boundaries_and_does_not_print_credentials(self):
        from cli.runtime_database import main
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": "sqlite://"}, clear=True), \
             patch("cli.runtime_database.os.execvpe") as execute:
            self.assertEqual(main(["python", "-c", "print('one argument')"]), 0)
        self.assertEqual(execute.call_args.args[1], ["python", "-c", "print('one argument')"])
