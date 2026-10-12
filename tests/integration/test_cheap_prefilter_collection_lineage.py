"""Preview real collected postings through immutable configured-source evidence."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import MockTransport, Response
from sqlalchemy import select, update
import unittest

from apps.api.cheap_prefilter import create_cheap_prefilter_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source
from tests.integration import test_prefilter_discover_worker as fixtures


class CheapPrefilterCollectionLineageTests(unittest.TestCase):
    setUp = fixtures.SavedSearchCaptureScoringTests.setUp
    tearDown = fixtures.SavedSearchCaptureScoringTests.tearDown
    _run = fixtures.SavedSearchCaptureScoringTests._run

    def _preview(self, store, criteria=None):
        app = FastAPI()
        app.include_router(create_cheap_prefilter_router(store))
        with TestClient(app) as client:
            response = client.post(
                "/profiles/fixture/search-profiles/selected-search/cheap-prefilter-preview",
                json={"criteria": criteria or {}},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _snapshot(self, store):
        with store.engine.connect() as connection:
            return tuple([dict(row) for row in connection.execute(
                select(table).order_by(table.c.id)).mappings()] for table in (
                    m.postings, m.posting_snapshots, m.posting_value_candidates,
                    m.source_quality_observations, m.jobs, m.search_profiles,
                    m.matches, m.audit, m.operations,
                ))

    def test_real_worker_collection_preview_is_read_only_and_survives_reopen(self):
        self.assertEqual(self._run(), [("1", "fixture")])
        before = self._snapshot(self.store)
        reopened = Store(self.store.url)
        try:
            for store in (self.store, reopened):
                with self.subTest(reopened=store is reopened):
                    result = self._preview(store)
                    self.assertEqual(result["stage_counts"], {"captured": 5, "prefiltered": 1})
                    self.assertEqual(result["candidate_count"], 1)
                    self.assertEqual(result["rejected_count"], 4)
                    self.assertEqual(self._snapshot(store), before)
        finally:
            reopened.engine.dispose()

    def test_preview_uses_selected_snapshot_not_mutable_job_or_endpoint_attribution(self):
        self._run(other_canonical_source=True)
        with self.store.engine.begin() as connection:
            posting = connection.execute(select(m.postings).order_by(m.postings.c.id)).mappings().first()
            # Another observation can change the current raw projection. The
            # selected collection still has its original immutable snapshot.
            connection.execute(update(m.postings).values(raw={"title": "Astronaut", "description": "Space"}))
            connection.execute(update(m.company_sources).values(collector_source_id=None))
            # A shared endpoint alone is insufficient proof of collection scope.
            connection.execute(m.postings.insert().values(
                id="unrelated-endpoint-row", job_id=posting["job_id"],
                source=posting["source"], source_url=posting["source_url"],
                source_job_id="unrelated", application_url=posting["application_url"],
                raw={"title": "Systems Administrator", "description": "Linux"},
                fetched_at=posting["fetched_at"],
            ))
        before = self._snapshot(self.store)
        result = self._preview(self.store)
        self.assertEqual(result["stage_counts"], {"captured": 5, "prefiltered": 1})
        self.assertEqual(self._snapshot(self.store), before)

    def test_repeated_collection_does_not_duplicate_preview_rows(self):
        self._run()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source_id)
                               .values(last_attempt=None))
        self._run(run_marker="second-collection")
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(m.posting_value_candidates)).all()), 10)
        result = self._preview(self.store)
        self.assertEqual(result["stage_counts"], {"captured": 5, "prefiltered": 1})

    def test_preview_uses_latest_selected_collection_values(self):
        self._run()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source_id)
                               .values(last_attempt=None))
        job = {"id": 1, "title": "Systems Administrator", "content": "<p>Maintain Windows services.</p>",
               "location": {"name": "Remote"}, "updated_at": "2026-10-02T00:00:00Z",
               "absolute_url": "https://job-boards.greenhouse.io/search-capture-fixture/jobs/1"}

        def transport(request):
            return Response(200, json={"jobs": [job], "meta": {"total": 1}}
                            if request.url.path.endswith("/jobs") else {"name": "Synthetic Employer"})

        collect_source(self.store, self.source_id, "fixture", transport=MockTransport(transport))
        before = self._snapshot(self.store)
        result = self._preview(self.store)
        self.assertEqual(result["stage_counts"], {"captured": 5, "prefiltered": 0})
        self.assertEqual(result["rejected_count"], 5)
        self.assertEqual(self._snapshot(self.store), before)

    def test_failed_collection_lineage_is_not_preview_evidence(self):
        self._run()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.source_quality_observations).values(succeeded=False))
        before = self._snapshot(self.store)
        result = self._preview(self.store)
        self.assertEqual(result["stage_counts"], {"captured": 0, "prefiltered": 0})
        self.assertEqual(self._snapshot(self.store), before)


if __name__ == "__main__":
    unittest.main()
