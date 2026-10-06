"""Crash-safe generated-artifact storage and quarantine controls."""
from pathlib import Path
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class AtomicArtifactWriteTests(unittest.TestCase):
    def test_interrupted_write_never_replaces_an_existing_artifact(self):
        from services.documents.artifacts import atomic_generate

        with tempfile.TemporaryDirectory(prefix="mjobs-atomic-artifact-") as directory:
            target = Path(directory) / "resume.pdf"
            target.write_bytes(b"previous immutable artifact")
            script = r"""
import os
import sys
from pathlib import Path
from services.documents.artifacts import atomic_generate

def interrupt(path):
    path.write_bytes(b"partial replacement")
    os._exit(73)

atomic_generate(Path(sys.argv[1]), interrupt)
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(target)],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 73, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(target.read_bytes(), b"previous immutable artifact")
            self.assertEqual(len(list(Path(directory).glob(".resume.pdf.*.tmp"))), 1)

    def test_local_inventory_command_reads_only_an_explicit_bound_workspace(self):
        from cli.document_artifacts import main
        from packages.persistence.store import Store

        with tempfile.TemporaryDirectory(prefix="mjobs-artifact-cli-") as directory:
            root = Path(directory)
            output = root / "documents"
            output.mkdir()
            store = Store(f"sqlite:///{(root / 'workspace.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            stdout = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    with patch.dict(os.environ, {"MJOBS_DATABASE_URL": str(store.engine.url)}):
                        result = main(["scan", "--output-dir", str(output)])
                self.assertEqual(result, 0)
                self.assertEqual(stdout.getvalue().strip(), "[]")
            finally:
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
