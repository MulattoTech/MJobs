"""Interview-prep pack schema stays additive and refuses lossy rollback."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, inspect, text

from packages.persistence import models as m


PARENT_REVISION = "d0c97315c313"
EXPECTED_MIGRATION = "20261008ip01"


class InterviewPreparationMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _upgrade(self, config, url):
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
            command.upgrade(config, "head")

    def _insert_pack(self, engine):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO career_profiles (id, name, email, version) "
                "VALUES ('profile-1', 'Synthetic', 'synthetic@example.test', 'profile-v1')"
            ))
            connection.execute(text(
                "INSERT INTO companies (id, name, domain) "
                "VALUES ('company-1', 'Synthetic Co', 'example.test')"
            ))
            connection.execute(text(
                "INSERT INTO canonical_jobs (id, company_id, requisition, title, description, "
                "skills, location, remote_type, attributes) VALUES "
                "('job-1', 'company-1', 'req-1', 'Synthetic role', 'Synthetic description', "
                "'[]', 'Remote', 'REMOTE', '{}')"
            ))
            connection.execute(text(
                "INSERT INTO interview_prep_packs (id, profile_id, job_id, pack_version, "
                "profile_version, job_fingerprint, selected_story_revision_ids, questions, "
                "talking_points, star_prompts, missing_evidence, export_text, created_at) "
                "VALUES ('pack-1', 'profile-1', 'job-1', 1, 'profile-v1', 'fingerprint-1', "
                "'[\"story-revision-1\"]', '[\"question\"]', '[\"talking point\"]', "
                "'[\"STAR prompt\"]', '[]', 'Synthetic export', '2026-10-08T00:00:00Z')"
            ))

    def test_single_head_adds_profile_scoped_versioned_pack_schema_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'interview-prep.db').as_posix()}"
            config = self._config(url)
            script = ScriptDirectory.from_config(config)
            heads = script.get_heads()
            self.assertEqual(heads, [EXPECTED_MIGRATION])
            self.assertEqual(script.get_revision(heads[0]).down_revision, PARENT_REVISION)

            engine = create_engine(url)
            try:
                self._upgrade(config, url)
                inspector = inspect(engine)
                self.assertTrue(inspector.has_table("interview_prep_packs"))
                columns = {
                    column["name"]: column
                    for column in inspector.get_columns("interview_prep_packs")
                }
                expected_columns = {
                    "id", "profile_id", "job_id", "pack_version", "profile_version",
                    "job_fingerprint", "selected_story_revision_ids", "questions",
                    "talking_points", "star_prompts", "missing_evidence", "export_text",
                    "created_at",
                }
                self.assertEqual(set(columns), expected_columns)
                self.assertEqual(set(m.interview_prep_packs.c.keys()), expected_columns)
                self.assertTrue(all(columns[name]["nullable"] is False for name in expected_columns))
                self.assertEqual(columns["pack_version"]["type"].__class__.__name__, "INTEGER")
                for name in (
                    "selected_story_revision_ids", "questions", "talking_points",
                    "star_prompts", "missing_evidence",
                ):
                    self.assertEqual(columns[name]["type"].__class__.__name__, "JSON")
                foreign_keys = {
                    fk["referred_table"]: fk
                    for fk in inspector.get_foreign_keys("interview_prep_packs")
                }
                self.assertEqual(set(foreign_keys), {"career_profiles", "canonical_jobs"})
                self.assertEqual(foreign_keys["career_profiles"]["options"].get("ondelete"), "CASCADE")
                self.assertNotIn("ondelete", foreign_keys["canonical_jobs"]["options"])
                self.assertTrue(any(
                    constraint["column_names"] == ["profile_id", "job_id", "pack_version"]
                    and constraint["name"] == "uq_interview_prep_pack_profile_job_version"
                    for constraint in inspector.get_unique_constraints("interview_prep_packs")
                ))
                check_names = {
                    item["name"] for item in inspector.get_check_constraints("interview_prep_packs")
                }
                model_check_names = {
                    constraint.name for constraint in m.interview_prep_packs.constraints
                    if isinstance(constraint, CheckConstraint)
                }
                self.assertEqual(check_names, model_check_names)
                self.assertIn("ck_interview_prep_pack_version_positive", check_names)

                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.downgrade(config, PARENT_REVISION)
                self.assertFalse(inspect(engine).has_table("interview_prep_packs"))
            finally:
                engine.dispose()

    def test_populated_downgrade_refuses_loss_and_keeps_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'interview-prep-preserve.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                self._upgrade(config, url)
                self.assertTrue(inspect(engine).has_table("interview_prep_packs"))
                self._insert_pack(engine)

                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}), self.assertRaisesRegex(
                    RuntimeError, "Refusing to discard interview-preparation packs"
                ):
                    command.downgrade(config, PARENT_REVISION)
                with engine.connect() as connection:
                    row = connection.execute(text(
                        "SELECT profile_id, job_id, pack_version, selected_story_revision_ids, "
                        "export_text FROM interview_prep_packs WHERE id = 'pack-1'"
                    )).one()
                    self.assertEqual(tuple(row), (
                        "profile-1", "job-1", 1, '["story-revision-1"]', "Synthetic export"
                    ))
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), EXPECTED_MIGRATION)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
