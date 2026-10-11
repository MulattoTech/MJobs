"""API review gates and live status for PERSONAL company discovery requests."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.requests import record_discovery_request
from tests.integration.test_spine import PROFILE


class DiscoveryRequestApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self._seed_request()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_request(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(id="company", name="Employer", domain=""))
            connection.execute(m.company_sources.insert().values(
                id="source", company_id="company", provider="rss", tenant="tenant",
                endpoint="https://employer.example/feed", discovered_automatically=True,
                collection_strategy="RSS", confidence=0.9, enabled=True,
                last_success="2026-10-03T00:00:00Z"))
            connection.execute(m.jobs.insert().values(
                id="job", company_id="company", requisition="req", title="Role",
                description="Description", skills=[], location="Remote", remote_type="REMOTE",
                attributes={"source_resolution": {"selected_candidate_id": "candidate"}}))
            connection.execute(m.postings.insert().values(
                id="posting", job_id="job", source="feed", source_job_id="req",
                source_url="https://employer.example/feed", application_url="https://employer.example/jobs/1",
                raw={}, fetched_at="2026-10-03T00:00:00Z"))
            connection.execute(m.posting_snapshots.insert().values(
                id="snapshot", posting_id="posting", raw={},
                sha256="a" * 64, retrieved_at="2026-10-03T00:00:00Z"))
            connection.execute(m.source_quality_observations.insert().values(
                id="quality", company_source_id="source", source_endpoint="https://employer.example/feed",
                observed_at="2026-10-03T00:00:00Z", succeeded=True, available=True))
            connection.execute(m.posting_value_candidates.insert().values(
                id="candidate", posting_id="posting", posting_snapshot_id="snapshot",
                company_source_id="source", quality_observation_id="quality",
                observed_at="2026-10-03T00:00:00Z",
                normalized={"attributes": {"source_endpoint": "https://employer.example/feed"}},
                application_url="https://employer.example/jobs/1"))
        profile = self.store.profile("fixture")
        with self.store.engine.begin() as connection:
            self.request_id = record_discovery_request(
                connection, "job", {"id": "fixture", "version": profile["version"]},
                is_new_company=True)

    def _client(self, mode="PERSONAL"):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode=mode))

    def test_get_exposes_live_review_status_and_revision(self):
        with self._client() as client:
            response = client.get("/company-discovery/requests")
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json()["requests"] if item["id"] == self.request_id)
        self.assertEqual(row["status"], "NEEDS_ORIGIN_REVIEW")
        self.assertEqual(row["reason"], "EMPLOYER_ORIGIN_UNVERIFIED")
        self.assertEqual(row["highest_match_score"], None)

    def test_fixture_mode_rejects_review_action(self):
        fixture = Store(f"sqlite:///{(self.root / 'fixture.db').as_posix()}")
        fixture.migrate()
        fixture.bind_workspace_mode("FIXTURE")
        try:
            with TestClient(create_app(
                    fixture, "http://127.0.0.1:8090", self.root,
                    "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
                response = client.post(
                    "/company-discovery/requests/missing/review",
                    json={"website": "https://employer.example", "revision": 1})
        finally:
            fixture.engine.dispose()
        self.assertEqual(response.status_code, 409)
        self.assertIn("personal workspace", response.text)

    def test_shared_ats_and_invalid_websites_are_rejected(self):
        with self._client() as client:
            for website in ("https://job-boards.greenhouse.io/employer", "not-a-url"):
                response = client.post(
                    f"/company-discovery/requests/{self.request_id}/review",
                    json={"website": website, "revision": 1})
                self.assertEqual(response.status_code, 409, website)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request_id)).mappings().one()
        self.assertEqual(row["state"], "NEEDS_ORIGIN_REVIEW")

    def test_stale_revision_is_rejected_without_changing_request(self):
        with self._client() as client:
            response = client.post(
                f"/company-discovery/requests/{self.request_id}/dismiss",
                json={"revision": 99})
        self.assertEqual(response.status_code, 409)
        self.assertIn("REVISION_CONFLICT", response.text)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request_id)).mappings().one()
        self.assertEqual(row["revision"], 1)
        self.assertEqual(row["state"], "NEEDS_ORIGIN_REVIEW")

    def test_valid_review_queues_durable_operation_and_get_reflects_live_state(self):
        with self._client() as client:
            response = client.post(
                f"/company-discovery/requests/{self.request_id}/review",
                json={"website": "https://employer.example", "revision": 1})
            self.assertEqual(response.status_code, 202)
            queued = response.json()
            self.assertEqual(queued["status"], "QUEUED")
            self.assertEqual(client.get("/company-discovery/requests").json()["requests"][0]["status"], "QUEUED")
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == queued["operation_id"])).mappings().one()
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request_id)).mappings().one()
        self.assertEqual(operation["kind"], "COMPANY_DISCOVER")
        self.assertEqual(operation["state"], "QUEUED")
        self.assertEqual(operation["params"]["fetch_mode"], "PUBLIC_HTTPS")
        self.assertEqual(request["state"], "QUEUED")
        self.assertEqual(request["revision"], 2)

    def test_explicit_identity_confirmation_is_strict_and_queues_request_company(self):
        with self._client() as client:
            invalid = client.post(
                f"/company-discovery/requests/{self.request_id}/review",
                json={"website": "https://employer.example", "revision": 1,
                      "confirm_company_identity": "true"})
            self.assertEqual(invalid.status_code, 422)
            accepted = client.post(
                f"/company-discovery/requests/{self.request_id}/review",
                json={"website": "https://employer.example", "revision": 1,
                      "confirm_company_identity": True})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == accepted.json()["operation_id"])).mappings().one()
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request_id)).mappings().one()
        self.assertEqual(operation["entity_id"], "company")
        self.assertEqual(request["authorization"], "USER_CONFIRMED_DOMAIN")
        self.assertEqual(request["domain_evidence"][0]["method"], "USER_CONFIRMED_DOMAIN")
        self.assertEqual(self.store.count("company_domains"), 0)


if __name__ == "__main__":
    unittest.main()
