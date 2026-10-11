"""The Alembic graph resolves to one head and upgrades a fresh SQLite store."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


EXPECTED_PARENTS = ("9b6d40aaf163", "f37a0c2e91b5")


class MigrationGraphTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def test_single_head_upgrades_fresh_sqlite_database(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'migration-graph.db').as_posix()}"
            config = self._config(url)
            script = ScriptDirectory.from_config(config)
            heads = script.get_heads()
            self.assertEqual(len(heads), 1, f"expected one migration head, found {heads}")

            merge = script.get_revision("e2a4c9d0b721")
            self.assertEqual(set(merge.down_revision), set(EXPECTED_PARENTS))
            self.assertIn(merge.revision, {revision.revision for revision in script.iterate_revisions(heads[0], "base")})

            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "head")

                inspector = inspect(engine)
                self.assertTrue(inspector.has_table("source_candidate_reviews"))
                columns = {column["name"] for column in inspector.get_columns("source_candidate_reviews")}
                self.assertTrue({
                    "api_feed_evidence_status",
                    "api_feed_evidence_reference",
                    "api_feed_evidence_note",
                    "access_terms_note",
                    "capabilities_limits_note",
                    "expected_value_maintenance_note",
                }.issubset(columns))
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                        heads[0],
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
