"""Fresh SQLite Store.migrate() includes separately registered persistence tables."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ResumeWorkspaceStoreMigrationTests(unittest.TestCase):
    def test_fresh_store_migrate_registers_resume_workspace_revisions(self):
        repository_root = Path(__file__).resolve().parents[2]
        script = """
import sys
from sqlalchemy import inspect
from packages.persistence.store import Store

store = Store(sys.argv[1])
try:
    store.migrate()
    tables = set(inspect(store.engine).get_table_names())
    assert "resume_workspace_revisions" in tables, tables
finally:
    store.engine.dispose()
"""
        with tempfile.TemporaryDirectory() as directory:
            database_path = (Path(directory) / "fresh.db").as_posix()
            database_url = f"sqlite:///{database_path}"
            result = subprocess.run(
                [sys.executable, "-c", script, database_url],
                cwd=repository_root,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
