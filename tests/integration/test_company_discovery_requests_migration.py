"""Company-discovery trigger requests migrate without invented history or loss."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m


class CompanyDiscoveryRequestsMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _legacy_rows(self, engine):
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO companies (id,name,domain) VALUES ('company','Employer','employer.example')"))
            connection.execute(text("INSERT INTO company_job_sources (id,company_id,provider,tenant,endpoint,discovered_automatically,last_success) VALUES ('source','company','rss','tenant','https://employer.example/feed',1,'2026-10-03T00:00:00Z')"))
            connection.execute(text("INSERT INTO canonical_jobs (id,company_id,requisition,title,description,skills,location,remote_type,attributes) VALUES ('job','company','req','Role','Description','[]','Remote','REMOTE','{}')"))
            connection.execute(text("INSERT INTO job_postings (id,job_id,source,source_job_id,source_url,application_url,raw,fetched_at) VALUES ('posting','job','feed','req','https://employer.example/feed','https://employer.example/jobs/1','{}','2026-10-03T00:00:00Z')"))
            connection.execute(text("INSERT INTO posting_snapshots (id,posting_id,raw,sha256,retrieved_at) VALUES ('snapshot','posting','{}','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','2026-10-03T00:00:00Z')"))
            connection.execute(text("INSERT INTO source_quality_observations (id,company_source_id,observed_at,succeeded,available) VALUES ('quality','source','2026-10-03T00:00:00Z',true,true)"))
            connection.execute(m.posting_value_candidates.insert().values(
                id="candidate", posting_id="posting", posting_snapshot_id="snapshot",
                company_source_id="source", quality_observation_id="quality",
                observed_at="2026-10-03T00:00:00Z", normalized={"legacy": True},
                application_url="https://employer.example/jobs/1"))

    def test_upgrade_preserves_existing_rows_without_fabricating_requests_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'legacy.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "b2d6f0a19c44")
                    self._legacy_rows(engine)
                    command.upgrade(config, "head")
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.jobs.c.id)).scalar_one(), "job")
                        self.assertEqual(connection.execute(select(m.posting_value_candidates.c.id)).scalar_one(), "candidate")
                        self.assertEqual(connection.execute(select(m.company_discovery_requests)).all(), [])
                    command.downgrade(config, "b2d6f0a19c44")
            finally:
                engine.dispose()

    def test_populated_request_refuses_downgrade_and_preserves_row(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'request.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "b2d6f0a19c44")
                    self._legacy_rows(engine)
                    command.upgrade(config, "head")
                    with engine.begin() as connection:
                        connection.execute(m.company_discovery_requests.insert().values(
                            id="request", company_id="company", company_source_id="source",
                            candidate_id="candidate", trigger_job_id="job", reasons=["NEW_DOMAIN"],
                            highest_match_score=88.5, created_at="2026-10-03T00:00:00Z",
                            last_seen_at="2026-10-03T00:00:00Z", state="NEEDS_ORIGIN_REVIEW",
                            selected_website="https://employer.example", authorization=None,
                            domain_evidence={"status": "UNVERIFIED"}, source_snapshot={"endpoint": "https://employer.example/feed"}))
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard company discovery requests"):
                        command.downgrade(config, "b2d6f0a19c44")
                    with engine.connect() as connection:
                        row = connection.execute(select(m.company_discovery_requests)).mappings().one()
                        self.assertEqual(row["id"], "request")
                        self.assertEqual(row["candidate_id"], "candidate")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
