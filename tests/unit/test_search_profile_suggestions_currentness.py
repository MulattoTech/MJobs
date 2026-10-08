"""Current-version suggestions must stay read-only for profiles and workers."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.learning.search_profile_suggestions import suggest_search_profile_titles
from tests.integration.test_spine import PROFILE


class SearchProfileSuggestionCurrentnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-suggestions-currentness-")
        database = Path(self.directory.name) / "jobs.db"
        self.store = Store(f"sqlite:///{database.as_posix()}")
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_stale_only_matches_are_omitted_without_changing_criteria_or_worker_state(self):
        original_version = self.store.profile("fixture")["version"]
        changed_profile = dict(PROFILE)
        changed_profile["name"] = "Updated profile"
        self.store.save_profile(changed_profile)

        job_id = "stale-match-job"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="stale-match-company", name="Synthetic Company", domain="example.test",
                parent_company_id=None, relationship_type=None,
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id="stale-match-company", requisition=job_id,
                title="Stale Infrastructure Engineer", description="", skills=[],
                location="", remote_type="UNKNOWN", attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id="stale-match", job_id=job_id, profile_id="fixture",
                profile_version=original_version, score=99, missing=[], components={},
            ))
            connection.execute(m.search_profiles.insert().values(
                id="saved-search", profile_id="fixture", name="Saved search",
                enabled=True, criteria={"title": "System Administrator", "remote": True},
                source_ids=["fixture-source"], created_at="2026-10-08T00:00:00Z",
                updated_at="2026-10-08T00:00:00Z",
            ))
            connection.execute(m.operations.insert().values(
                id="queued-worker-operation", kind="search", entity_id="saved-search",
                params={"profile_id": "fixture"}, state="QUEUED", error=None,
                updated_at="2026-10-08T00:00:00Z", attempt_count=0,
                cancel_requested=False, lease_expires_at=None, last_attempt_at=None,
                dead_lettered_at=None, discarded_at=None,
            ))

        def read_state():
            with self.store.engine.connect() as connection:
                criteria = connection.execute(
                    select(m.search_profiles).where(m.search_profiles.c.id == "saved-search")
                ).mappings().one()
                operation = connection.execute(
                    select(m.operations).where(m.operations.c.id == "queued-worker-operation")
                ).mappings().one()
            return dict(criteria), dict(operation)

        before = read_state()
        suggestions = suggest_search_profile_titles(self.store, "fixture")
        after = read_state()

        self.assertEqual(suggestions, [])
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
