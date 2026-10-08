"""Interview-prep pack references survive an SQLite connection restart."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m


class InterviewPreparationMigrationConstraintsTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def test_versioned_pack_and_story_revision_refs_survive_sqlite_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'interview-prep-reopen.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "head")
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
                    connection.execute(m.facts.insert().values(
                        id="fact-row-1", profile_id="profile-1", version="profile-v1",
                        fact_id="fact-1", kind="EXPERIENCE", text="Synthetic user claim",
                        source="user-provided", verified=True, user_approved=True,
                        confidence=0.95,
                    ))
                    fact_before = connection.execute(select(
                        m.facts.c.fact_id, m.facts.c.text, m.facts.c.verified,
                        m.facts.c.user_approved, m.facts.c.confidence,
                    ).where(m.facts.c.id == "fact-row-1")).one()
                    connection.execute(m.interview_prep_packs.insert().values(
                        id="pack-v1", profile_id="profile-1", job_id="job-1",
                        pack_version=1, profile_version="profile-v1",
                        job_fingerprint="a" * 64,
                        selected_story_revision_ids=["story-revision-7"],
                        questions=[{"prompt": "Describe a relevant challenge."}],
                        talking_points=[{"text": "Synthetic fact-backed point", "fact_ids": ["fact-1"]}],
                        star_prompts=[{"question": "What was the result?"}],
                        missing_evidence=[], export_text="Synthetic export v1\n",
                        created_at="2026-10-09T10:00:00Z",
                    ))
                    connection.execute(m.interview_prep_packs.insert().values(
                        id="pack-v2", profile_id="profile-1", job_id="job-1",
                        pack_version=2, profile_version="profile-v1",
                        job_fingerprint="a" * 64,
                        selected_story_revision_ids=["story-revision-7", "story-revision-9"],
                        questions=[{"prompt": "Describe a second relevant challenge."}],
                        talking_points=[{"text": "Synthetic fact-backed point", "fact_ids": ["fact-1"]}],
                        star_prompts=[{"question": "What changed?"}],
                        missing_evidence=[{"topic": "leadership", "reason": "No selected story."}],
                        export_text="Synthetic export v2\n",
                        created_at="2026-10-09T10:01:00Z",
                    ))

                engine.dispose()
                reopened = create_engine(url)
                try:
                    with reopened.connect() as connection:
                        rows = connection.execute(select(m.interview_prep_packs).where(
                            m.interview_prep_packs.c.profile_id == "profile-1",
                            m.interview_prep_packs.c.job_id == "job-1",
                        ).order_by(m.interview_prep_packs.c.pack_version)).mappings().all()
                        self.assertEqual([row["pack_version"] for row in rows], [1, 2])
                        self.assertEqual(rows[0]["selected_story_revision_ids"], ["story-revision-7"])
                        self.assertEqual(rows[1]["selected_story_revision_ids"], [
                            "story-revision-7", "story-revision-9",
                        ])
                        self.assertEqual(rows[0]["talking_points"][0]["fact_ids"], ["fact-1"])
                        self.assertEqual(rows[1]["export_text"], "Synthetic export v2\n")
                        fact_after = connection.execute(select(
                            m.facts.c.fact_id, m.facts.c.text, m.facts.c.verified,
                            m.facts.c.user_approved, m.facts.c.confidence,
                        ).where(m.facts.c.id == "fact-row-1")).one()
                        self.assertEqual(tuple(fact_after), tuple(fact_before))
                finally:
                    reopened.dispose()
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
