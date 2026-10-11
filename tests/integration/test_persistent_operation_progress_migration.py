"""The persistent-operation progress schema is additive and preserves queued history."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


PARENT_REVISION = "20261008ip01"
EXPECTED_MIGRATION = "20261011op01"


class PersistentOperationProgressMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def test_additive_migration_preserves_existing_operations_and_initializes_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'operations.db').as_posix()}"
            config = self._config(url)
            script = ScriptDirectory.from_config(config)
            self.assertEqual(script.get_revision(EXPECTED_MIGRATION).down_revision, PARENT_REVISION)
            self.assertEqual(len(script.get_heads()), 1)
            self.assertIn(EXPECTED_MIGRATION, {revision.revision for revision in script.walk_revisions()})

            with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                command.upgrade(config, PARENT_REVISION)
                engine = create_engine(url)
                try:
                    with engine.begin() as connection:
                        connection.execute(text(
                            "INSERT INTO worker_operations "
                            "(id, kind, entity_id, params, state, error, updated_at, attempt_count, "
                            "cancel_requested) VALUES "
                            "('op-existing', 'SEARCH', 'profile-1', '{}', 'QUEUED', NULL, "
                            "'2026-10-10T00:00:00Z', 0, 0)"
                        ))
                finally:
                    engine.dispose()
                command.upgrade(config, "head")

            engine = create_engine(url)
            try:
                columns = {column["name"] for column in inspect(engine).get_columns("worker_operations")}
                self.assertTrue({
                    "current_step", "progress", "started_at", "completed_at", "retry_count",
                    "recovery_metadata",
                }.issubset(columns))
                with engine.connect() as connection:
                    row = connection.execute(text(
                        "SELECT state, progress, started_at, completed_at, retry_count, recovery_metadata "
                        "FROM worker_operations WHERE id='op-existing'"
                    )).mappings().one()
                self.assertEqual(row["state"], "QUEUED")
                self.assertEqual(row["progress"], 0)
                self.assertIsNone(row["started_at"])
                self.assertIsNone(row["completed_at"])
                self.assertEqual(row["retry_count"], 0)
                metadata = row["recovery_metadata"]
                self.assertEqual(json.loads(metadata) if isinstance(metadata, str) else metadata, {})
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
