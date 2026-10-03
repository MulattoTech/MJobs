"""Posting value candidates migrate without rewriting or discarding evidence."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m


class PostingValueCandidatesMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _legacy_rows(self, engine):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO companies (id,name,domain) VALUES "
                "('company','Employer','employer.example')"
            ))
            connection.execute(text(
                "INSERT INTO company_job_sources "
                "(id,company_id,provider,tenant,endpoint,discovered_automatically,last_success) "
                "VALUES ('company-source','company','rss','tenant','https://employer.example/jobs',1,'2026-10-01T00:00:00Z')"
            ))
            connection.execute(text(
                "INSERT INTO canonical_jobs "
                "(id,company_id,requisition,title,description,skills,location,remote_type) VALUES "
                "('job','company','req-1','Network Administrator','Python systems','[]','Remote','REMOTE')"
            ))
            connection.execute(text(
                "INSERT INTO job_postings "
                "(id,job_id,source,source_job_id,source_url,application_url,raw,fetched_at) VALUES "
                "('posting','job','company-feed:company-source','req-1',"
                "'https://employer.example/jobs','https://employer.example/jobs/1','{}','2026-10-01T00:00:00Z')"
            ))
            connection.execute(text(
                "INSERT INTO posting_snapshots "
                "(id,posting_id,raw,sha256,retrieved_at) VALUES "
                "('snapshot','posting','{}','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "'2026-10-01T00:00:00Z')"
            ))

    def test_upgrade_preserves_legacy_posting_values_and_empty_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'legacy.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "f083a019c241")
                    self._legacy_rows(engine)
                    command.upgrade(config, "head")
                    with engine.connect() as connection:
                        posting = connection.execute(select(m.postings)).mappings().one()
                        snapshot = connection.execute(select(m.posting_snapshots)).mappings().one()
                        source = connection.execute(select(m.company_sources)).mappings().one()
                        self.assertEqual((posting["id"], posting["application_url"]), ("posting", "https://employer.example/jobs/1"))
                        self.assertEqual((snapshot["id"], snapshot["posting_id"]), ("snapshot", "posting"))
                        self.assertEqual((source["id"], source["endpoint"]), ("company-source", "https://employer.example/jobs"))
                        self.assertEqual(connection.execute(select(m.posting_value_candidates)).all(), [])
                    command.downgrade(config, "f083a019c241")
            finally:
                engine.dispose()

    def test_populated_candidates_refuse_downgrade_and_remain_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'candidate.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "f083a019c241")
                    self._legacy_rows(engine)
                    command.upgrade(config, "head")
                    with engine.begin() as connection:
                        connection.execute(m.source_quality_observations.insert().values(
                            id="quality", company_source_id="company-source", observed_at="2026-10-01T00:00:00Z",
                            succeeded=True, available=True, enumerated=1, accepted=1,
                            completeness_sum=1.0, salary_known=1,
                        ))
                        connection.execute(m.posting_value_candidates.insert().values(
                            id="candidate", posting_id="posting", posting_snapshot_id="snapshot",
                            company_source_id="company-source", quality_observation_id="quality",
                            observed_at="2026-10-01T00:00:00Z", normalized={"salary": 90000},
                            application_url="https://employer.example/jobs/1",
                        ))
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard posting value candidates"):
                        command.downgrade(config, "f083a019c241")
                    with engine.connect() as connection:
                        candidate = connection.execute(select(m.posting_value_candidates)).mappings().one()
                        self.assertEqual(candidate["id"], "candidate")
                        self.assertEqual(candidate["normalized"], {"salary": 90000})
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
