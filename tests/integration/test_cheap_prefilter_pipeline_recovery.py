"""Stored postings remain readable after cheap-prefilter outcomes and failures."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.cheap_prefilter_pipeline import prefilter_search_profile_postings


class CheapPrefilterPipelineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database_path = (Path(self.temp.name) / "prefilter-recovery.db").as_posix()
        self.store = Store(f"sqlite:///{database_path}")
        self.store.migrate()
        self._seed_postings()

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _seed_postings(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="employer", name="Example Employer", domain="employer.example"))
            for index, title in enumerate(("Systems Administrator", "Infrastructure Engineer"), 1):
                job_id = f"job-{index}"
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="employer", requisition=f"req-{index}",
                    title=title, description=f"Linux operations role {index}", skills=["Linux"],
                    location="Remote", remote_type="REMOTE", attributes={}))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{index}", job_id=job_id, source="fixture",
                    source_job_id=f"req-{index}", source_url="https://employer.example/feed",
                    application_url=f"https://employer.example/jobs/{index}",
                    raw={"provider_id": f"provider-{index}"},
                    fetched_at="2026-10-08T00:00:00Z"))

    def _stored_postings(self):
        query = (select(m.postings.c.id, m.jobs.c.title, m.jobs.c.description, m.postings.c.raw)
                 .select_from(m.postings.join(m.jobs)).order_by(m.postings.c.id))
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings().all()]

    def test_empty_candidate_result_does_not_remove_stored_postings(self):
        original_rows = self._stored_postings()

        result = prefilter_search_profile_postings(
            original_rows,
            {"criteria": {"include_titles": ["No matching role"]}},
        )

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["rejected_count"], len(original_rows))
        self.assertEqual(self._stored_postings(), original_rows)

    def test_prefilter_validation_failure_leaves_stored_postings_readable(self):
        original_rows = self._stored_postings()

        with self.assertRaisesRegex(ValueError, "include_titles"):
            prefilter_search_profile_postings(
                original_rows,
                {"criteria": {"include_titles": "Systems Administrator"}},
            )

        self.assertEqual(self._stored_postings(), original_rows)

    def test_deferred_postings_remain_persisted_for_later_processing(self):
        original_rows = self._stored_postings()

        result = prefilter_search_profile_postings(
            original_rows, {"criteria": {}}, max_candidates=1
        )

        deferred_ids = [row["id"] for row in result["deferred"]]
        self.assertEqual(len(deferred_ids), 1)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(
            [row["id"] for row in self._stored_postings()],
            [row["id"] for row in original_rows],
        )
        self.assertIn(deferred_ids[0], {row["id"] for row in self._stored_postings()})


if __name__ == "__main__":
    unittest.main()
