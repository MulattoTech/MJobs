"""Source candidate capture remains passive and profile scoped."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration.test_spine import PROFILE


class SourceCandidateCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-source-candidate-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'candidate.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_manual_capture_catalogues_destination_without_overwriting_reported_origin(self):
        payload = {
            "profile_id": "fixture",
            "request_id": "source-candidate-manual-1",
            "title": "Systems Administrator",
            "company": "Example Employer",
            "description": "Maintain Linux infrastructure.",
            "reference_url": "https://www.linkedin.com/jobs/view/123?tracking=private-token",
            "application_url": "https://talenthop.com/apply/role-123?session=private-token#form",
            "source_details": {"discovery_origin": "LinkedIn"},
        }
        captured = self.client.post("/jobs/manual", json=payload)
        self.assertEqual(captured.status_code, 201, captured.text)

        listed = self.client.get("/source-candidates", params={"profile_id": "fixture"})

        self.assertEqual(listed.status_code, 200, listed.text)
        candidates = listed.json()["candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["domain"], "talenthop.com")
        self.assertEqual(candidate["state"], "NEEDS_REVIEW")
        self.assertFalse(candidate["collection_enabled"])
        observation = candidate["observations"][0]
        self.assertEqual(observation["discovery_origin"], "LinkedIn")
        self.assertEqual(observation["origin_attribution"], "USER_REPORTED")
        self.assertEqual(observation["application_url"], "https://talenthop.com/apply/role-123")
        self.assertNotIn("private-token", str(candidate))

        replay = self.client.post("/jobs/manual", json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        reread = self.client.get("/source-candidates", params={"profile_id": "fixture"})
        self.assertEqual(len(reread.json()["candidates"]), 1)
        self.assertEqual(len(reread.json()["candidates"][0]["observations"]), 1)
        isolated = self.client.get("/source-candidates", params={"profile_id": "other"})
        self.assertEqual(isolated.status_code, 200, isolated.text)
        self.assertEqual(isolated.json()["candidates"], [])

    def test_malformed_nested_hop_metadata_stays_one_explicit_unknown(self):
        response = self.client.post("/jobs/manual", json={
            "profile_id": "fixture", "request_id": "malformed-source-hops",
            "title": "Unknown role", "company": "Unknown employer",
            "description": "Details were unavailable.",
            "application_url": "https://apply.example.invalid/role",
            "source_details": {"observed_hops": "untrusted non-list metadata"},
        })
        self.assertEqual(response.status_code, 201, response.text)
        candidates = self.client.get("/source-candidates", params={"profile_id": "fixture"}).json()["candidates"]
        self.assertEqual(candidates[0]["observations"][0]["observed_hops"], [{"state": "UNKNOWN"}])

    def test_url_only_report_creates_profile_scoped_candidate_without_job_metadata(self):
        payload = {
            "profile_id": "fixture", "request_id": "url-only-source-report",
            "application_url": "https://www.talenthop.com/apply/role?session=private-token",
            "discovery_origin": "LinkedIn", "observed_hops": [],
        }
        captured = self.client.post("/source-candidates", json=payload)
        self.assertEqual(captured.status_code, 201, captured.text)
        candidate = captured.json()["candidate"]
        self.assertEqual(candidate["domain"], "talenthop.com")
        observation = candidate["observations"][0]
        self.assertIsNone(observation["job_id"])
        self.assertIsNone(observation["application_id"])
        self.assertEqual(observation["discovery_origin"], "LinkedIn")
        self.assertEqual(observation["observed_hops"], [{"state": "UNKNOWN"}])
        self.assertNotIn("private-token", str(candidate))
        replay = self.client.post("/source-candidates", json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        conflict = self.client.post("/source-candidates", json={
            **payload, "application_url": "https://different-provider.invalid/role",
        })
        self.assertEqual(conflict.status_code, 409, conflict.text)
        reread = self.client.get("/source-candidates", params={"profile_id": "fixture"})
        self.assertEqual(len(reread.json()["candidates"][0]["observations"]), 1)

    def test_supported_feed_import_catalogues_external_application_origin_idempotently(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "catalogue-feed-employer", "name": "Feed Employer",
                "domain": "employer.example",
            })
        endpoint = "https://employer.example/jobs.json"
        source = upsert_company_job_source(
            self.store, company_id="catalogue-feed-employer", provider="rss",
            tenant_identifier="feed-board", public_api_url=endpoint,
            collection_strategy="PUBLIC_JSON",
        )
        items = [{
            "id": "role-1", "title": "Systems Administrator",
            "description": "Maintain synthetic Linux infrastructure.",
            "url": "https://jobs.example-ats.invalid/apply/role-1?session=private-token",
        }]

        def fail_receipt(_connection):
            raise RuntimeError("synthetic feed import rollback")

        with self.assertRaisesRegex(RuntimeError, "synthetic feed import rollback"):
            import_company_feed_jobs(
                self.store, company_id="catalogue-feed-employer", source_id=source["id"],
                provider="rss", tenant="feed-board", endpoint=endpoint,
                strategy="PUBLIC_JSON", items=items, profile_id="fixture",
                extra_profile_ids=("other",), import_receipt=fail_receipt,
            )
        rolled_back = self.client.get(
            "/source-candidates", params={"profile_id": "fixture"}).json()["candidates"]
        self.assertEqual(rolled_back, [])

        imported = import_company_feed_jobs(
            self.store, company_id="catalogue-feed-employer", source_id=source["id"],
            provider="rss", tenant="feed-board", endpoint=endpoint,
            strategy="PUBLIC_JSON", items=items, profile_id="fixture",
            extra_profile_ids=("other",),
        )
        replayed = import_company_feed_jobs(
            self.store, company_id="catalogue-feed-employer", source_id=source["id"],
            provider="rss", tenant="feed-board", endpoint=endpoint,
            strategy="PUBLIC_JSON", items=items, profile_id="fixture",
            extra_profile_ids=("other",),
        )

        self.assertEqual(imported, 1)
        self.assertEqual(replayed, 1)
        fixture_candidates = self.client.get(
            "/source-candidates", params={"profile_id": "fixture"}).json()["candidates"]
        other_candidates = self.client.get(
            "/source-candidates", params={"profile_id": "other"}).json()["candidates"]
        self.assertEqual(len(fixture_candidates), 1)
        self.assertEqual(len(other_candidates), 1)
        for candidate in (*fixture_candidates, *other_candidates):
            self.assertEqual(candidate["domain"], "jobs.example-ats.invalid")
            self.assertFalse(candidate["collection_enabled"])
            self.assertEqual(len(candidate["observations"]), 1)
            observation = candidate["observations"][0]
            self.assertEqual(observation["capture_method"], "SUPPORTED_COLLECTION")
            self.assertEqual(observation["origin_attribution"], "UNKNOWN")
            self.assertIsNone(observation["discovery_origin"])
            self.assertEqual(observation["evidence_status"], "PARTIAL")
            self.assertEqual(observation["source_url"], endpoint)
            self.assertEqual(observation["application_url"],
                             "https://jobs.example-ats.invalid/apply/role-1")
            self.assertEqual(observation["observed_hops"], [{"state": "UNKNOWN"}])
            self.assertNotIn("private-token", str(candidate))
        self.assertIsNotNone(fixture_candidates[0]["observations"][0]["job_id"])
        self.assertEqual(self.store.count("source_candidates"), 2)

    def test_supported_feed_does_not_catalogue_its_already_enrolled_employer_origin(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "catalogue-first-party-employer", "name": "Feed Employer",
                "domain": "employer.example",
            })
        endpoint = "https://employer.example/jobs.json"
        source = upsert_company_job_source(
            self.store, company_id="catalogue-first-party-employer", provider="rss",
            tenant_identifier="feed-board", public_api_url=endpoint,
            collection_strategy="PUBLIC_JSON",
        )
        import_company_feed_jobs(
            self.store, company_id="catalogue-first-party-employer", source_id=source["id"],
            provider="rss", tenant="feed-board", endpoint=endpoint,
            strategy="PUBLIC_JSON", profile_id="fixture", items=[{
                "id": "role-2", "title": "Systems Administrator",
                "description": "Maintain synthetic Linux infrastructure.",
                "url": "https://employer.example/jobs/role-2",
            }],
        )

        listed = self.client.get("/source-candidates", params={"profile_id": "fixture"})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["candidates"], [])


    def test_external_application_preserves_origin_and_review_history_is_profile_scoped(self):
        payload = {
            "profile_id": "fixture", "request_id": "source-candidate-external-1",
            "company": "Example Employer", "title": "Systems Administrator",
            "source": "BROWSER", "applied_at": "2026-10-06T14:00:00Z",
            "application_url": "https://talenthop.com/apply/role-123?session=private-token",
            "source_url": "https://www.linkedin.com/jobs/view/123?tracking=private-token",
            "discovery_origin": "LinkedIn", "observed_hops": [],
        }
        captured = self.client.post("/applications/external", json=payload)
        self.assertEqual(captured.status_code, 201, captured.text)
        candidate = self.client.get("/source-candidates", params={"profile_id": "fixture"}).json()["candidates"][0]
        observation = candidate["observations"][0]
        self.assertEqual(candidate["domain"], "talenthop.com")
        self.assertEqual(observation["discovery_origin"], "LinkedIn")
        self.assertEqual(observation["origin_attribution"], "USER_REPORTED")
        self.assertEqual(observation["application_id"], captured.json()["application_id"])
        self.assertEqual(observation["observed_hops"], [{"state": "UNKNOWN"}])
        self.assertNotIn("private-token", str(candidate))

        review_payload = {
            "state": "BLOCKED", "site_type": "ATS", "reason": "Read-only inspection was denied; integration is unsupported.",
            "api_feed_evidence_status": "BLOCKED",
            "api_feed_evidence_reference": "https://talenthop.com/api/jobs?token=private-token",
            "api_feed_evidence_note": "The documented job endpoint returned an access denial during prior read-only inspection.",
            "access_terms_note": "Terms and automated access permission have not been verified.",
            "capabilities_limits_note": "No public job feed is currently evidenced; application destination only.",
            "expected_value_maintenance_note": "Potential value is unknown; maintenance cost cannot be estimated without an authorized feed.",
            "request_id": "source-candidate-review-1",
        }
        review = self.client.put(f"/profiles/fixture/source-candidates/{candidate['id']}/review", json=review_payload)
        self.assertEqual(review.status_code, 200, review.text)
        reread = self.client.get("/source-candidates", params={"profile_id": "fixture"}).json()["candidates"][0]
        self.assertEqual(reread["state"], "BLOCKED")
        self.assertEqual(reread["site_type"], "ATS")
        self.assertEqual(reread["review_history"][0]["site_type"], "ATS")
        history = reread["review_history"][0]
        self.assertEqual(history["api_feed_evidence_status"], "BLOCKED")
        self.assertEqual(history["api_feed_evidence_reference"], "https://talenthop.com/api/jobs")
        self.assertIn("access denial", history["api_feed_evidence_note"])
        self.assertIn("not been verified", history["access_terms_note"])
        self.assertIn("No public job feed", history["capabilities_limits_note"])
        self.assertIn("unknown", history["expected_value_maintenance_note"])
        self.assertEqual(history["profile_id"], "fixture")
        self.assertFalse(reread["collection_enabled"])
        self.assertEqual(len(reread["review_history"]), 1)
        changed_replay = self.client.put(f"/profiles/fixture/source-candidates/{candidate['id']}/review", json={
            **review_payload, "api_feed_evidence_note": "Different content with the same request id.",
        })
        self.assertEqual(changed_replay.status_code, 409, changed_replay.text)
        invalid_evidence = self.client.put(f"/profiles/fixture/source-candidates/{candidate['id']}/review", json={
            "state": "NEEDS_REVIEW", "site_type": "UNKNOWN", "reason": "Evidence state requires detail.",
            "api_feed_evidence_status": "PUBLIC_DOCUMENTED", "api_feed_evidence_note": "documented feed",
            "access_terms_note": "not checked", "capabilities_limits_note": "unknown",
            "expected_value_maintenance_note": "unknown", "request_id": "source-candidate-invalid-evidence",
        })
        self.assertEqual(invalid_evidence.status_code, 422, invalid_evidence.text)
        documented = self.client.put(f"/profiles/fixture/source-candidates/{candidate['id']}/review", json={
            "state": "NEEDS_REVIEW", "site_type": "JOB_BOARD", "reason": "Public documentation reviewed.",
            "api_feed_evidence_status": "PUBLIC_DOCUMENTED",
            "api_feed_evidence_reference": "https://docs.example.invalid/jobs?token=public-reference-token",
            "api_feed_evidence_note": "Documentation describes a public JSON endpoint.",
            "access_terms_note": "Documented terms permit public read access.",
            "capabilities_limits_note": "Provides title, company, location, and application URL.",
            "expected_value_maintenance_note": "High expected value; maintenance is moderate.",
            "request_id": "source-candidate-review-2",
        })
        self.assertEqual(documented.status_code, 200, documented.text)
        history = documented.json()["review_history"]
        self.assertEqual(len(history), 2)
        latest = history[-1]
        self.assertEqual(latest["api_feed_evidence_status"], "PUBLIC_DOCUMENTED")
        self.assertEqual(latest["api_feed_evidence_reference"], "https://docs.example.invalid/jobs")
        self.assertNotIn("public-reference-token", str(history))
        hidden = self.client.put(f"/profiles/other/source-candidates/{candidate['id']}/review", json={
            "state": "REJECTED", "reason": "Foreign profile", "site_type": "ATS",
            "api_feed_evidence_status": "UNKNOWN", "api_feed_evidence_reference": None,
            "api_feed_evidence_note": "not reviewed",
            "access_terms_note": "unknown", "capabilities_limits_note": "unknown",
            "expected_value_maintenance_note": "unknown", "request_id": "foreign-review",
        })
        self.assertEqual(hidden.status_code, 404)

        replay = self.client.post("/applications/external", json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        reread = self.client.get("/source-candidates", params={"profile_id": "fixture"}).json()["candidates"][0]
        self.assertEqual(len(reread["observations"]), 1)
        self.assertEqual(len(reread["review_history"]), 2)


if __name__ == "__main__":
    unittest.main()
