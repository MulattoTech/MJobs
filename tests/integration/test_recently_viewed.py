"""Explicit visits are profile-private, bounded and unrelated to applications."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.pipeline import discover
from services.tracking.recently_viewed import MAX_AGE_DAYS, MAX_VIEWS_PER_PROFILE, record_job_view
from tests.integration.test_spine import PROFILE


class RecentlyViewedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-recently-viewed-")
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'trial.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other",
                                 "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def path(self, profile="fixture"):
        return f"/profiles/{profile}/jobs/recently-viewed"

    def visit(self, job_id=None, profile="fixture"):
        return self.client.post(f"/profiles/{profile}/jobs/{job_id or self.job_id}/view")

    def test_explicit_open_only_separate_from_tracking_and_applications(self):
        before = self.client.get(self.path()).json()
        self.assertEqual(before["items"], [])
        self.assertEqual(before["max_items"], MAX_VIEWS_PER_PROFILE)
        self.assertEqual(before["retention_days"], MAX_AGE_DAYS)
        self.assertEqual(self.client.get(f"/jobs/{self.job_id}",
                                         params={"profile_id": "fixture"}).status_code, 200)
        self.assertEqual(self.client.get(self.path()).json()["items"], [])

        with self.store.engine.connect() as connection:
            dispositions_before = connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action.in_(("JOB_DISPOSITION", "JOB_NOTE")))).scalar_one()
            applications_before = connection.execute(select(func.count()).select_from(
                m.applications)).scalar_one()

        first = self.visit()
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["job_id"], self.job_id)
        second = self.visit()
        self.assertEqual(second.status_code, 201, second.text)
        rows = self.client.get(self.path()).json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], self.job_id)
        self.assertTrue(rows[0]["available"])
        self.assertTrue(rows[0]["title"])
        self.assertTrue(rows[0]["company"])
        self.assertEqual(self.client.get(
            f"/profiles/fixture/jobs/{self.job_id}/tracking").json()["disposition"], "ACTIVE")
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action.in_(("JOB_DISPOSITION", "JOB_NOTE")))).scalar_one(),
                             dispositions_before)
            self.assertEqual(connection.execute(select(func.count()).select_from(
                m.applications)).scalar_one(), applications_before)

    def test_closed_canonical_jobs_are_unavailable_without_mutating_visits(self):
        self.assertEqual(self.visit().status_code, 201)
        original = self.client.get(self.path()).json()["items"][0]
        with self.store.engine.connect() as connection:
            attributes = connection.execute(select(m.jobs.c.attributes).where(
                m.jobs.c.id == self.job_id)).scalar_one()
            visits_before = connection.execute(select(m.profile_job_views)).mappings().all()
            applications_before = connection.execute(select(func.count()).select_from(
                m.applications)).scalar_one()
            audit_before = connection.execute(select(func.count()).select_from(m.audit)).scalar_one()

        for status in ("CLOSED", "FILLED", "EXPIRED", "REMOVED", " closed "):
            with self.subTest(status=status):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).where(m.jobs.c.id == self.job_id).values(
                        attributes={**attributes, "status": status}))
                response = self.client.get(self.path())
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(response.json()["items"]), 1)
                item = response.json()["items"][0]
                self.assertFalse(item["available"])
                self.assertEqual(item["unavailable_reason"], "NO_LONGER_OPEN")
                self.assertEqual(item["title"], original["title"])
                self.assertEqual(item["company"], original["company"])
                self.assertEqual(item["viewed_at"], original["viewed_at"])

        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.profile_job_views)).mappings().all(),
                             visits_before)
            self.assertEqual(connection.execute(select(func.count()).select_from(
                m.applications)).scalar_one(), applications_before)
            self.assertEqual(connection.execute(select(func.count()).select_from(m.audit)).scalar_one(),
                             audit_before)
        self.assertEqual(self.client.get(
            f"/profiles/fixture/jobs/{self.job_id}/tracking").json()["disposition"], "ACTIVE")

        for status in ("ACTIVE", "POSSIBLY_ACTIVE", "UNKNOWN", "not a structured status", None):
            with self.subTest(nonterminal_status=status):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).where(m.jobs.c.id == self.job_id).values(
                        attributes={**attributes, "status": status}))
                item = self.client.get(self.path()).json()["items"][0]
                self.assertTrue(item["available"])
                self.assertIsNone(item.get("unavailable_reason"))
                self.assertEqual(item["viewed_at"], original["viewed_at"])

    def test_profile_isolation_clear_and_database_reopen(self):
        self.assertEqual(self.visit().status_code, 201)
        self.assertEqual(self.visit(profile="other").status_code, 201)
        fixture_rows = self.client.get(self.path()).json()["items"]
        other_rows = self.client.get(self.path("other")).json()["items"]
        self.assertEqual(len(fixture_rows), 1)
        self.assertEqual(len(other_rows), 1)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        self.assertEqual(self.client.get(self.path()).json()["items"], fixture_rows)
        self.assertEqual(self.client.get(self.path("other")).json()["items"], other_rows)
        cleared = self.client.delete(self.path())
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(cleared.json(), {"profile_id": "fixture", "cleared": 1})
        self.assertEqual(self.client.get(self.path()).json()["items"], [])
        self.assertEqual(self.client.get(self.path("other")).json()["items"], other_rows)
        self.assertEqual(self.client.delete(self.path()).json()["cleared"], 0)

    def test_bounded_order_dedup_and_retention(self):
        with self.store.engine.begin() as connection:
            company_id = connection.execute(select(m.jobs.c.company_id).where(
                m.jobs.c.id == self.job_id)).scalar_one()
            connection.execute(m.jobs.insert(), [{
                "id": f"recent-job-{i:03d}", "company_id": company_id,
                "requisition": f"RV{i:03d}", "title": f"Synthetic job {i}",
                "description": "Fixture only", "skills": [], "location": "Remote",
                "remote_type": "REMOTE_US", "attributes": {},
            } for i in range(MAX_VIEWS_PER_PROFILE + 5)])
        for i in range(MAX_VIEWS_PER_PROFILE + 5):
            record_job_view(self.store, "fixture", f"recent-job-{i:03d}")
        rows = self.client.get(self.path()).json()["items"]
        self.assertEqual(len(rows), MAX_VIEWS_PER_PROFILE)
        self.assertEqual(rows[0]["job_id"], "recent-job-104")
        self.assertEqual(rows[-1]["job_id"], "recent-job-005")
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(func.count()).select_from(
                m.profile_job_views).where(
                    m.profile_job_views.c.profile_id == "fixture")).scalar_one(),
                             MAX_VIEWS_PER_PROFILE)

        self.assertEqual(self.visit("recent-job-010").status_code, 201)
        rows = self.client.get(self.path()).json()["items"]
        self.assertEqual(rows[0]["job_id"], "recent-job-010")
        self.assertEqual(len(rows), MAX_VIEWS_PER_PROFILE)
        self.assertEqual(sum(row["job_id"] == "recent-job-010" for row in rows), 1)
        old_time = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS + 1)).isoformat()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.profile_job_views).where(
                m.profile_job_views.c.profile_id == "fixture").values(viewed_at=old_time))
        self.assertEqual(self.client.get(self.path()).json()["items"], [])
        self.assertEqual(self.visit("recent-job-104").status_code, 201)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(func.count()).select_from(
                m.profile_job_views).where(
                    m.profile_job_views.c.profile_id == "fixture")).scalar_one(), 1)

    def test_missing_job_profile_invalid_limit_and_stale_row(self):
        self.assertEqual(self.visit("unknown-job").status_code, 404)
        self.assertEqual(self.visit(profile="unknown-profile").status_code, 404)
        self.assertEqual(self.client.get(self.path("unknown-profile")).status_code, 404)
        self.assertEqual(self.client.delete(self.path("unknown-profile")).status_code, 404)
        self.assertEqual(self.client.get(self.path(), params={"limit": 101}).status_code, 422)
        self.assertEqual(self.client.get(self.path(), params={"limit": 0}).status_code, 422)
        with self.store.engine.begin() as connection:
            connection.execute(m.profile_job_views.insert().values(
                id=identity("recent-job-view", "fixture", "removed-job"),
                profile_id="fixture", job_id="removed-job",
                viewed_at=datetime.now(timezone.utc).isoformat(), visit_order=1,
            ))
        stale = self.client.get(self.path()).json()["items"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["job_id"], "removed-job")
        self.assertFalse(stale[0]["available"])
        self.assertEqual(stale[0]["unavailable_reason"], "MISSING")
        self.assertIsNone(stale[0]["title"])
        self.assertIsNone(stale[0]["company"])


if __name__ == "__main__":
    unittest.main()
