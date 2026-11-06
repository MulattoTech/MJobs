"""Migration safety for private, bounded job visit history."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from packages.persistence import models as m


class RecentlyViewedMigrationTests(unittest.TestCase):
    def config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def test_empty_upgrade_and_downgrade(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-view-migration-") as directory:
            url = f"sqlite:///{(Path(directory) / 'empty.db').as_posix()}"
            config = self.config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "20261025di01")
                    command.upgrade(config, "head")
                    self.assertIn("profile_job_views", inspect(engine).get_table_names())
                    command.downgrade(config, "20261025di01")
                    self.assertNotIn("profile_job_views", inspect(engine).get_table_names())
            finally:
                engine.dispose()

    def test_populated_downgrade_refuses_to_discard_views(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-view-migration-") as directory:
            url = f"sqlite:///{(Path(directory) / 'populated.db').as_posix()}"
            config = self.config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "head")
                    with engine.begin() as connection:
                        connection.execute(m.profiles.insert().values(
                            id="profile", name="Synthetic", email="synthetic@example.invalid",
                            version="v1"))
                        connection.execute(m.profile_job_views.insert().values(
                            id="visit", profile_id="profile", job_id="synthetic-job",
                            viewed_at="2026-11-04T00:00:00+00:00", visit_order=1))
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard recently viewed"):
                        command.downgrade(config, "20261025di01")
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.profile_job_views.c.job_id)).scalar_one(),
                                         "synthetic-job")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
