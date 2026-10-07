"""Explicit AI document review records survive migration and block lossy rollback."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from packages.persistence import models as m


class AIDocumentReviewMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _add_review(self, engine):
        with engine.begin() as connection:
            connection.execute(m.profiles.insert().values(
                id="profile", name="Synthetic", email="synthetic@example.test", version="v1"))
            connection.execute(m.ai_evaluation_runs.insert().values(
                id="run", profile_id="profile", profile_version="v1",
                policy={"max_document_candidates": 1}, task_provider="ollama",
                task_model="synthetic", evaluated_candidate_count=1,
                created_at="2026-10-06T00:00:00Z"))
            connection.execute(m.ai_document_reviews.insert().values(
                id="review", profile_id="profile", run_id="run", job_id="job",
                reviewed_at="2026-10-06T00:00:00Z"))

    def test_upgrade_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'empty.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "a4c19e7d603b")
                    command.upgrade(config, "head")
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.ai_document_reviews)).all(), [])
                    command.downgrade(config, "a4c19e7d603b")
            finally:
                engine.dispose()

    def test_populated_review_refuses_downgrade_and_preserves_record(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'populated.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "a4c19e7d603b")
                    command.upgrade(config, "head")
                    self._add_review(engine)
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard explicit AI document review"):
                        command.downgrade(config, "a4c19e7d603b")
                    with engine.connect() as connection:
                        record = connection.execute(select(m.ai_document_reviews)).mappings().one()
                        self.assertEqual(record["job_id"], "job")
                        self.assertEqual(record["run_id"], "run")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
