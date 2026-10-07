"""External application reports reuse canonical jobs without rewriting history."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.dedupe.routes import load_job_routes
from tests.integration.test_spine import PROFILE


class ExternalApplicationCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-external-application-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'capture.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.jobs = self.store.jobs("fixture")
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_route_url_precedes_conflicting_company_and_title_and_is_idempotent(self):
        job = self.jobs[0]
        route = load_job_routes(self.store, job["id"])[0]
        payload = {
            "profile_id": "fixture", "request_id": "external-route-report-1",
            "company": "Conflicting Synthetic Employer", "title": "Conflicting Synthetic Role",
            "application_url": route["url"], "source": "REFERRAL",
            "applied_at": "2026-10-01T16:30:00-05:00",
        }

        response = self.client.post("/applications/external", json=payload)

        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertEqual(result["job_id"], job["id"])
        self.assertEqual(result["application"]["state"], "EXTERNALLY_APPLIED")
        self.assertEqual(result["tracking"]["status"], "APPLIED")
        self.assertEqual(result["tracking"]["attribution"], "EXTERNAL_USER_REPORTED")
        self.assertEqual(result["tracking"]["external_reports"][0]["source"], "REFERRAL")
        self.assertEqual(result["tracking"]["external_reports"][0]["applied_at"],
                         "2026-10-01T21:30:00Z")

        replay = self.client.post("/applications/external", json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertFalse(replay.json()["created"])
        self.assertEqual(replay.json()["application_id"], result["application_id"])
        self.assertEqual(replay.json()["tracking"], result["tracking"])

        later = self.client.put(
            f"/profiles/fixture/applications/{result['application_id']}/tracking/status",
            json={"status": "OFFER", "request_id": "later-offer"})
        self.assertEqual(later.json()["status"], "OFFER")
        earlier_external = {**payload, "request_id": "older-external-report",
                            "applied_at": "2026-09-25T09:00:00Z", "source": "BROWSER"}
        recorded = self.client.post("/applications/external", json=earlier_external)
        self.assertEqual(recorded.status_code, 201, recorded.text)
        self.assertEqual(recorded.json()["tracking"]["status"], "OFFER")
        self.assertEqual(self.store.application(result["application_id"])["state"],
                         "EXTERNALLY_APPLIED")

    def test_exact_company_title_fallback_and_minimal_job_creation_are_provenance_marked(self):
        job = self.jobs[0]
        payload = {
            "profile_id": "fixture", "request_id": "external-fallback-1",
            "company": job["company"].upper(), "title": job["title"].lower(),
            "source": "MANUAL", "applied_at": "2026-10-02T10:00:00Z",
        }
        matched = self.client.post("/applications/external", json=payload)
        self.assertEqual(matched.status_code, 201, matched.text)
        self.assertEqual(matched.json()["job_id"], job["id"])
        self.assertEqual(matched.json()["reconciliation"], "COMPANY_TITLE_EXACT")

        new_job = self.client.post("/applications/external", json={
            **payload, "request_id": "external-new-minimal-job",
            "company": "Synthetic Unlisted Employer", "title": "Network Administrator",
            "application_url": "https://apply.example.test/network-admin",
            "job_description": None,
        })
        self.assertEqual(new_job.status_code, 201, new_job.text)
        created = self.store.job(new_job.json()["job_id"])
        self.assertEqual(created["title"], "Network Administrator")
        self.assertEqual(created["company"], "Synthetic Unlisted Employer")
        self.assertIn("description was not provided", created["description"].lower())
        self.assertEqual(new_job.json()["reconciliation"], "CREATED_MINIMAL_JOB")
        self.assertEqual(new_job.json()["application"]["target"], "EXTERNAL_REPORTED")

    def test_profile_isolation_invalid_urls_and_request_id_conflicts_fail_closed(self):
        payload = {
            "profile_id": "fixture", "request_id": "external-guard-1",
            "company": "Synthetic Employer", "title": "Systems Administrator",
            "application_url": "https://apply.example.test/role",
            "source": "MANUAL", "applied_at": "2026-10-02T10:00:00Z",
        }
        created = self.client.post("/applications/external", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        foreign = self.client.get(
            f"/profiles/other/applications/{created.json()['application_id']}/tracking")
        self.assertEqual(foreign.status_code, 404)
        conflict = self.client.post("/applications/external", json={
            **payload, "company": "Changed Company"})
        self.assertEqual(conflict.status_code, 409)
        invalid = self.client.post("/applications/external", json={
            **payload, "request_id": "external-invalid-url", "application_url": "javascript:alert(1)"})
        self.assertEqual(invalid.status_code, 422)
        no_route = self.client.post("/applications/external", json={
            **payload, "request_id": "external-no-route", "company": "No Match Ltd",
            "title": "Unlisted Role", "application_url": None,
        })
        self.assertEqual(no_route.status_code, 422)
        naive_time = self.client.post("/applications/external", json={
            **payload, "request_id": "external-naive-time",
            "applied_at": "2026-10-02T10:00:00",
        })
        self.assertEqual(naive_time.status_code, 422)
        listed = self.client.get("/applications", params={"profile_id": "fixture"})
        self.assertEqual(len(listed.json()["applications"]), 1)
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_concurrent_conflicting_request_id_reserves_once_before_job_creation(self):
        barrier = Barrier(2)
        payloads = [
            {"profile_id": "fixture", "request_id": "concurrent-external-key",
             "company": f"Synthetic Employer {index}", "title": f"Role {index}",
             "application_url": f"https://apply.example.test/role-{index}",
             "source": "MANUAL", "applied_at": "2026-10-03T10:00:00Z"}
            for index in range(2)
        ]

        def submit(payload):
            with TestClient(self.client.app) as client:
                barrier.wait(timeout=5)
                return client.post("/applications/external", json=payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, payloads))

        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
        self.assertEqual(self.store.count("applications"), 1)
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_capture_preserves_existing_prepared_application_classification(self):
        from services.documents.generate import prepare

        job = self.jobs[1]
        application_id = prepare(self.store, job["id"], "fixture", self.root)
        before = self.store.application(application_id)
        route = load_job_routes(self.store, job["id"])[0]
        saved = self.client.put(
            f"/profiles/fixture/applications/{application_id}/tracking/status",
            json={"status": "SAVED", "request_id": "saved-before-external-report"})
        self.assertEqual(saved.json()["status"], "SAVED")
        response = self.client.post("/applications/external", json={
            "profile_id": "fixture", "request_id": "external-after-preparation",
            "company": job["company"], "title": job["title"],
            "application_url": route["url"], "source": "BROWSER",
            "applied_at": "2026-10-03T12:00:00Z",
        })
        self.assertEqual(response.status_code, 201, response.text)
        after = self.store.application(application_id)
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["target"], before["target"])
        self.assertEqual(after["confirmation"], before["confirmation"])
        self.assertEqual(response.json()["tracking"]["status"], "APPLIED")
        self.assertEqual(response.json()["tracking"]["attribution"], "EXTERNAL_USER_REPORTED")
        self.assertEqual(response.json()["tracking"]["external_reports"][0]["source"], "BROWSER")

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(self.root / 'capture.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        reopened = self.client.get(
            f"/profiles/fixture/applications/{application_id}/tracking")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["external_reports"][0]["source"], "BROWSER")
        self.assertEqual(reopened.json()["status"], "APPLIED")
