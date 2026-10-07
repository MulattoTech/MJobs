"""Failed Alembic upgrades roll back and can be safely retried."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url


TARGET_REVISION = "b2d6f0a19c44"
PARENT_REVISION = "a7c4e1b92d30"
POSTGRES_URL_ENV = "MJOBS_MIGRATION_RECOVERY_POSTGRES_URL"


class MigrationRecoveryTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _exercise_interrupted_upgrade(self, url):
        config = self._config(url)
        engine = create_engine(url)
        database = make_url(url).database
        try:
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS recovery_sentinel"))
                connection.execute(text(
                    "CREATE TABLE recovery_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                ))
                connection.execute(text(
                    "INSERT INTO recovery_sentinel (id, value) VALUES (1, 'preserve-me')"
                ))

            with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                command.upgrade(config, PARENT_REVISION)
                script = ScriptDirectory.from_config(config)
                interrupted = []

                def interrupt_after_create(connection, cursor, statement, parameters, context, executemany):
                    if (connection.engine.url.database == database
                            and statement.lstrip().upper().startswith(
                                "CREATE TABLE SOURCE_APPLICATION_LINK_OBSERVATIONS"
                            )):
                        interrupted.append(statement)
                        raise RuntimeError("synthetic interruption after migration DDL")

                event.listen(Engine, "after_cursor_execute", interrupt_after_create)
                try:
                    with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                        command.upgrade(config, TARGET_REVISION)
                finally:
                    event.remove(Engine, "after_cursor_execute", interrupt_after_create)

                self.assertEqual(len(interrupted), 1, "fault injection must fire after target DDL executes")
                self.assertFalse(inspect(engine).has_table("source_application_link_observations"))
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                        PARENT_REVISION,
                    )
                    self.assertEqual(
                        connection.execute(text("SELECT value FROM recovery_sentinel WHERE id=1")).scalar_one(),
                        "preserve-me",
                    )

                command.upgrade(config, "head")

            self.assertTrue(inspect(engine).has_table("source_application_link_observations"))
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                    script.get_current_head(),
                )
                self.assertEqual(
                    connection.execute(text("SELECT value FROM recovery_sentinel WHERE id=1")).scalar_one(),
                    "preserve-me",
                )
        finally:
            engine.dispose()

    def test_interrupted_sqlite_upgrade_preserves_data_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'recovery.db').as_posix()}"
            self._exercise_interrupted_upgrade(url)

    @unittest.skipUnless(os.environ.get(POSTGRES_URL_ENV), "disposable PostgreSQL URL not configured")
    def test_interrupted_postgresql_upgrade_preserves_data_and_can_resume(self):
        url = os.environ[POSTGRES_URL_ENV]
        parsed = make_url(url)
        self.assertTrue(parsed.drivername.startswith("postgresql"))
        self.assertEqual(parsed.database, "mjobs_migration_recovery")
        self._exercise_interrupted_upgrade(url)


if __name__ == "__main__":
    unittest.main()
