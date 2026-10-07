"""Dead-letter migration adds metadata without dropping existing work."""
import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, update

from packages.persistence import models as m


class DeadLetterMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def test_upgrade_preserves_existing_operation_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'operations.db').as_posix()}"
            config, engine = self._config(url), create_engine(url)
            try:
                command.upgrade(config, "e5019c4a7b20")
                with engine.begin() as connection:
                    connection.execute(m.operations.insert().values(
                        id="operation-1", kind="DISCOVER", entity_id="profile-1", params={},
                        state="QUEUED", error=None, updated_at="2026-10-07T00:00:00Z",
                        attempt_count=0, cancel_requested=False, lease_expires_at=None))
                command.upgrade(config, "head")
                with engine.connect() as connection:
                    row = connection.execute(select(m.operations)).mappings().one()
                self.assertEqual(row["id"], "operation-1")
                self.assertEqual(row["state"], "QUEUED")
                self.assertIsNone(row["last_attempt_at"])
                self.assertIsNone(row["dead_lettered_at"])
                self.assertIsNone(row["discarded_at"])
                command.downgrade(config, "e5019c4a7b20")
                self.assertNotIn("dead_lettered_at", {
                    column["name"] for column in inspect(engine).get_columns("worker_operations")})
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.operations.c.id)).scalar_one(), "operation-1")
            finally:
                engine.dispose()

    def test_populated_dead_letter_refuses_lossy_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'dead-letter.db').as_posix()}"
            config, engine = self._config(url), create_engine(url)
            try:
                command.upgrade(config, "e5019c4a7b20")
                command.upgrade(config, "head")
                with engine.begin() as connection:
                    connection.execute(m.operations.insert().values(
                        id="operation-2", kind="DISCOVER", entity_id="profile-1", params={},
                        state="DEAD_LETTERED", error="RETRY_LIMIT", updated_at="2026-10-07T00:00:00Z",
                        attempt_count=3, cancel_requested=False, lease_expires_at=None,
                        last_attempt_at="2026-10-07T00:00:00Z", dead_lettered_at="2026-10-07T00:00:00Z"))
                with self.assertRaisesRegex(RuntimeError, "Refusing to discard dead-letter state"):
                    command.downgrade(config, "e5019c4a7b20")
                self.assertIn("dead_lettered_at", {
                    column["name"] for column in inspect(engine).get_columns("worker_operations")})
                with engine.connect() as connection:
                    row = connection.execute(select(m.operations).where(
                        m.operations.c.id == "operation-2")).mappings().one()
                    self.assertEqual(row["state"], "DEAD_LETTERED")
                    self.assertEqual(row["error"], "RETRY_LIMIT")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
