"""Team 19 connected recovery proof for selected saved-search discovery."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.operations import recover_expired
from workers.tasks import execute_operation


class FleetT19SearchRecoveryTests(unittest.TestCase):
    @staticmethod
    def _job_filters(score):
        return {
            "q": "", "title": "", "company": "", "location": "",
            "salary_min": "", "salary_max": "", "min_match_score": score,
            "experience_level": "", "employment_type": "", "skill": "",
            "education_requirement": "", "clearance_level": "",
            "posted_within_days": "", "remote_type": "", "provider": "",
            "sort": "score",
        }

    @staticmethod
    def _audit_snapshot(store):
        with store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(m.audit).where(m.audit.c.entity_id.in_(["fixture", "t19-job-search"])).order_by(
                    m.audit.c.timestamp, m.audit.c.id,
                )
            ).mappings()]

    def _api_store(self, root):
        store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(dict(PROFILE))
        app = create_app(store, "http://127.0.0.1:1", root, operation_mode="FIXTURE")
        return store, TestClient(app, raise_server_exceptions=False)

    def _save_disabled_filter_search(self, client, score="60"):
        return client.put("/search-profiles", json={
            "id": "t19-job-search", "profile_id": "fixture", "name": "T19 Jobs",
            "enabled": False, "job_filters": self._job_filters(score),
        })

    def test_explicit_filter_clear_allows_deliberate_enable_conversion(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t19-filter-convert-") as directory:
            root = Path(directory)
            store, client = self._api_store(root)
            try:
                saved = self._save_disabled_filter_search(client)
                self.assertEqual(saved.status_code, 200, saved.text)

                converted = client.put("/search-profiles", json={
                    "id": "t19-job-search", "profile_id": "fixture", "name": "T19 Jobs",
                    "enabled": True, "job_filters": None,
                })
                self.assertEqual(converted.status_code, 200, converted.text)
                readback = client.get("/search-profiles/t19-job-search")
                self.assertEqual(readback.status_code, 200, readback.text)
                self.assertTrue(readback.json()["enabled"])
                self.assertNotIn("job_filters", readback.json()["criteria"])
            finally:
                client.close()
                store.engine.dispose()

    def test_partial_disabled_filter_edit_preserves_disabled_state(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t19-filter-partial-") as directory:
            root = Path(directory)
            store, client = self._api_store(root)
            try:
                saved = self._save_disabled_filter_search(client)
                self.assertEqual(saved.status_code, 200, saved.text)

                edited = client.put("/search-profiles", json={
                    "id": "t19-job-search", "profile_id": "fixture", "name": "T19 Jobs",
                    "job_filters": self._job_filters("75"),
                })
                self.assertEqual(edited.status_code, 200, edited.text)
                readback = client.get("/search-profiles/t19-job-search")
                self.assertFalse(readback.json()["enabled"])
                self.assertEqual(readback.json()["criteria"]["job_filters"]["min_match_score"], "75")
            finally:
                client.close()
                store.engine.dispose()

    def test_rejected_enable_with_filters_leaves_search_and_audit_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t19-filter-reject-") as directory:
            root = Path(directory)
            store, client = self._api_store(root)
            reopened = None
            try:
                saved = self._save_disabled_filter_search(client)
                self.assertEqual(saved.status_code, 200, saved.text)
                before = self._audit_snapshot(store)

                rejected = client.put("/search-profiles", json={
                    "id": "t19-job-search", "profile_id": "fixture", "name": "T19 Jobs",
                    "enabled": True, "job_filters": self._job_filters("75"),
                })
                self.assertEqual(rejected.status_code, 422, rejected.text)

                reopened = Store(store.url)
                reopened_app = create_app(
                    reopened, "http://127.0.0.1:1", root, operation_mode="FIXTURE",
                )
                with TestClient(reopened_app, raise_server_exceptions=False) as reopened_client:
                    readback = reopened_client.get("/search-profiles/t19-job-search")
                    self.assertEqual(readback.status_code, 200, readback.text)
                    self.assertFalse(readback.json()["enabled"])
                    self.assertEqual(readback.json()["criteria"]["job_filters"]["min_match_score"], "60")
                self.assertEqual(self._audit_snapshot(reopened), before)
            finally:
                client.close()
                if reopened is not None:
                    reopened.engine.dispose()
                store.engine.dispose()

    def test_audit_failure_rolls_back_partial_filter_update_after_reopen(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t19-filter-interrupt-") as directory:
            root = Path(directory)
            store, client = self._api_store(root)
            reopened = None
            try:
                saved = self._save_disabled_filter_search(client)
                self.assertEqual(saved.status_code, 200, saved.text)
                before = self._audit_snapshot(store)
                with patch.object(store, "_audit", side_effect=RuntimeError("synthetic audit interruption")):
                    interrupted = client.put("/search-profiles", json={
                        "id": "t19-job-search", "profile_id": "fixture", "name": "T19 Jobs",
                        "enabled": False, "job_filters": self._job_filters("80"),
                    })
                self.assertEqual(interrupted.status_code, 500, interrupted.text)

                reopened = Store(store.url)
                reopened_app = create_app(
                    reopened, "http://127.0.0.1:1", root, operation_mode="FIXTURE",
                )
                with TestClient(reopened_app, raise_server_exceptions=False) as reopened_client:
                    readback = reopened_client.get("/search-profiles/t19-job-search")
                    self.assertEqual(readback.status_code, 200, readback.text)
                    self.assertFalse(readback.json()["enabled"])
                    self.assertEqual(readback.json()["criteria"]["job_filters"]["min_match_score"], "60")
                self.assertEqual(self._audit_snapshot(reopened), before)
            finally:
                client.close()
                if reopened is not None:
                    reopened.engine.dispose()
                store.engine.dispose()

    def test_selected_search_survives_expired_worker_and_reads_back_filter_history(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t19-search-recovery-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                store.save_profile(dict(PROFILE))
                store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture"})
                criteria = {
                    "id": "t19-selected", "profile_id": "fixture", "name": "T19 selected",
                    "enabled": True, "include_titles": ["Python Developer"],
                    "required_keywords": ["Python"], "source_ids": [],
                }
                save_search_profile(store, criteria)
                with fixture_server() as origin:
                    app = create_app(
                        store, origin, root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
                    )
                    with TestClient(app) as client:
                        queued = client.post("/discovery", json={
                            "profile_id": "fixture", "search_profile_id": "t19-selected",
                            "request_id": "t19-expired-worker-recovery",
                        })
                    self.assertEqual(queued.status_code, 202, queued.text)
                    operation_id = queued.json()["operation_id"]

                    # Model a worker process disappearing after its durable claim.
                    with store.engine.begin() as connection:
                        connection.execute(m.operations.update().where(
                            m.operations.c.id == operation_id,
                        ).values(
                            state="RUNNING", attempt_count=1,
                            started_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            last_attempt_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            lease_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                        ))
                    self.assertEqual(recover_expired(store), 1)
                    with store.engine.connect() as connection:
                        recovered = connection.execute(select(m.operations).where(
                            m.operations.c.id == operation_id,
                        )).mappings().one()
                    self.assertEqual(recovered["state"], "QUEUED")
                    self.assertEqual(recovered["recovery_metadata"]["action"], "RESUME")

                    with patch.dict(os.environ, {
                        "MJOBS_DATABASE_URL": store.url,
                        "MJOBS_MOCK_ORIGIN": origin,
                        "MJOBS_OUTPUT_DIR": str(root),
                        "MJOBS_OPERATION_MODE": "FIXTURE",
                    }):
                        result = execute_operation(operation_id)
                    self.assertEqual(result["status"], "COMPLETED", result)

                    with TestClient(app) as client:
                        jobs = client.get("/jobs", params={
                            "profile_id": "fixture", "search_profile_id": "t19-selected",
                            "sort": "score",
                        })
                        history = client.get("/profiles/fixture/search-history")
                        other_history = client.get("/profiles/other/search-history")
                    self.assertEqual(jobs.status_code, 200, jobs.text)
                    self.assertEqual([row["title"] for row in jobs.json()["jobs"]], ["Python Developer"])
                    self.assertEqual(history.status_code, 200, history.text)
                    self.assertEqual(other_history.status_code, 200, other_history.text)
                    self.assertEqual(other_history.json()["items"], [])
                    items = history.json()["items"]
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["id"], f"{operation_id}:2")
                    self.assertEqual(items[0]["status"], "COMPLETED")
                    self.assertEqual(items[0]["filters"]["include_titles"], ["Python Developer"])
                    self.assertEqual(items[0]["filters"]["required_keywords"], ["Python"])
                    self.assertNotIn("password", str(items[0]).casefold())
            finally:
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
