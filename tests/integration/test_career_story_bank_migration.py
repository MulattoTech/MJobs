"""Additive story-bank migration and data-preserving downgrade behavior."""
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


PARENT_REVISION = "c7d4a91e205f"
EXPECTED_MIGRATION = "d0c97315c313"


class CareerStoryBankMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _upgrade(self, config, url):
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
            command.upgrade(config, "head")

    def test_single_head_adds_profile_scoped_revision_schema_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'story-schema.db').as_posix()}"
            config = self._config(url)
            script = ScriptDirectory.from_config(config)
            heads = script.get_heads()
            self.assertEqual(heads, [EXPECTED_MIGRATION])
            revision = script.get_revision(heads[0])
            self.assertEqual(revision.down_revision, PARENT_REVISION)

            engine = create_engine(url)
            try:
                self._upgrade(config, url)
                inspector = inspect(engine)
                self.assertTrue(inspector.has_table("career_stories"))
                columns = {column["name"]: column for column in inspector.get_columns("career_stories")}
                expected_columns = {
                    "id", "profile_id", "story_key", "revision", "profile_version", "kind",
                    "title", "text", "fact_ids", "fact_fingerprints", "tags", "allowed_uses",
                    "review_status", "review_note", "created_at", "reviewed_at",
                }
                self.assertEqual(set(columns), expected_columns)
                self.assertEqual(set(m.career_stories.c.keys()), expected_columns)
                self.assertTrue(all(columns[name]["nullable"] is False for name in (
                    "id", "profile_id", "story_key", "revision", "profile_version", "kind",
                    "title", "text", "fact_ids", "fact_fingerprints", "tags", "allowed_uses",
                    "review_status", "created_at",
                )))
                self.assertTrue(columns["review_note"]["nullable"])
                self.assertTrue(columns["reviewed_at"]["nullable"])
                self.assertTrue(any(
                    fk["referred_table"] == "career_profiles"
                    and fk["options"].get("ondelete") == "CASCADE"
                    for fk in inspector.get_foreign_keys("career_stories")
                ))
                self.assertTrue(any(
                    constraint["column_names"] == ["profile_id", "story_key", "revision"]
                    and constraint["name"] == "uq_career_story_profile_key_revision"
                    for constraint in inspector.get_unique_constraints("career_stories")
                ))
                self.assertIn("ix_career_stories_profile_created", {
                    index["name"] for index in inspector.get_indexes("career_stories")
                })
                self.assertIn("ix_career_stories_profile_created", {
                    index.name for index in m.career_stories.indexes
                })
                check_names = {item["name"] for item in inspector.get_check_constraints("career_stories")}
                self.assertEqual(check_names, {
                    constraint.name for constraint in m.career_stories.constraints
                    if isinstance(constraint, CheckConstraint)
                })
                self.assertTrue({
                    "ck_career_story_revision_positive", "ck_career_story_kind",
                    "ck_career_story_review_status", "ck_career_story_review_timestamp",
                }.issubset(check_names))

                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.downgrade(config, PARENT_REVISION)
                self.assertFalse(inspect(engine).has_table("career_stories"))
            finally:
                engine.dispose()

    def test_populated_downgrade_refuses_loss_and_keeps_story_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'story-preserve.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                self._upgrade(config, url)
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO career_profiles (id, name, email, version) "
                        "VALUES ('profile', 'Synthetic', 'synthetic@example.test', 'profile-v1')"
                    ))
                    connection.execute(text(
                        "INSERT INTO career_stories (id, profile_id, story_key, revision, "
                        "profile_version, kind, title, text, fact_ids, fact_fingerprints, tags, "
                        "allowed_uses, review_status, review_note, created_at, reviewed_at) "
                        "VALUES ('story-r1', 'profile', 'story-key', 1, 'profile-v1', "
                        "'STAR_STORY', 'Synthetic story', 'User-authored synthetic story', "
                        "'[\"project-x\"]', '[{\"fact_id\":\"project-x\",\"fingerprint\":\"abc\"}]', "
                        "'[\"synthetic\"]', '[\"RESUME\"]', 'PENDING', NULL, "
                        "'2026-10-08T00:00:00Z', NULL)"
                    ))

                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}), self.assertRaisesRegex(
                    RuntimeError, "Refusing to discard career story revisions"
                ):
                    command.downgrade(config, PARENT_REVISION)
                with engine.connect() as connection:
                    story = connection.execute(text(
                        "SELECT profile_id, story_key, revision, title, fact_ids "
                        "FROM career_stories WHERE id = 'story-r1'"
                    )).one()
                    self.assertEqual(tuple(story), (
                        "profile", "story-key", 1, "Synthetic story", '["project-x"]'
                    ))
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), EXPECTED_MIGRATION)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
