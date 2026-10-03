"""Source application-link evidence migrates without fabricated history or loss."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m


class ApplicationLinkMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _legacy_rows(self, engine):
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO companies (id,name,domain) VALUES ('company','Employer','employer.example')"))
            connection.execute(text(
                "INSERT INTO company_job_sources (id,company_id,provider,tenant,endpoint,discovered_automatically,last_success) "
                "VALUES ('company-source','company','rss','tenant','https://employer.example/jobs',1,'2026-10-01T00:00:00Z')"))
            connection.execute(text(
                "INSERT INTO canonical_jobs (id,company_id,requisition,title,description,skills,location,remote_type) "
                "VALUES ('job','company','req-1','Network Administrator','Python systems','[]','Remote','REMOTE')"))
            connection.execute(text(
                "INSERT INTO job_postings (id,job_id,source,source_job_id,source_url,application_url,raw,fetched_at) "
                "VALUES ('posting','job','company-feed:company-source','req-1','https://employer.example/jobs',"
                "'https://employer.example/jobs/1','{}','2026-10-01T00:00:00Z')"))
            connection.execute(text(
                "INSERT INTO posting_snapshots (id,posting_id,raw,sha256,retrieved_at) VALUES "
                "('snapshot','posting','{}','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','2026-10-01T00:00:00Z')"))

    def _candidate(self, connection):
        connection.execute(m.source_quality_observations.insert().values(
            id="quality", company_source_id="company-source", observed_at="2026-10-01T00:00:00Z",
            succeeded=True, available=True, enumerated=1, accepted=1,
            completeness_sum=1.0, salary_known=1))
        connection.execute(m.posting_value_candidates.insert().values(
            id="candidate", posting_id="posting", posting_snapshot_id="snapshot",
            company_source_id="company-source", quality_observation_id="quality",
            observed_at="2026-10-01T00:00:00Z", normalized={"title": "Network Administrator"},
            application_url="https://employer.example/jobs/1"))

    def test_legacy_rows_untouched_empty_link_table_and_empty_downgrade(self):
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
                        self.assertEqual(posting["application_url"], "https://employer.example/jobs/1")
                        self.assertEqual(snapshot["id"], "snapshot")
                        self.assertEqual(connection.execute(select(m.source_application_link_observations)).all(), [])
                    command.downgrade(config, "a7c4e1b92d30")
            finally:
                engine.dispose()

    def test_populated_link_evidence_refuses_downgrade_and_preserves_row(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'evidence.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "f083a019c241")
                    self._legacy_rows(engine)
                    command.upgrade(config, "head")
                    with engine.begin() as connection:
                        self._candidate(connection)
                        connection.execute(m.source_application_link_observations.insert().values(
                            id="link-observation", candidate_id="candidate",
                            observed_at="2026-10-02T00:00:00Z",
                            requested_url="https://employer.example/jobs/1",
                            final_url="https://employer.example/jobs/1", result="ACCURATE",
                            reason="HTTP_200_SAME_ORIGIN", response_status=200,
                            response_sha256="b" * 64, identity_evidence={"company": "verified"}))
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard source application-link evidence"):
                        command.downgrade(config, "a7c4e1b92d30")
                    with engine.connect() as connection:
                        row = connection.execute(select(m.source_application_link_observations)).mappings().one()
                        self.assertEqual(row["id"], "link-observation")
                        self.assertEqual(row["candidate_id"], "candidate")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
