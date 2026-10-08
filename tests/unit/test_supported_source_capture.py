"""Supported collection results enter the profile-scoped source review queue."""
import tempfile
import unittest
from pathlib import Path

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.source_candidates.catalogue import (
    list_candidates,
    record_supported_collection,
)


class SupportedSourceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-supported-source-capture-")
        root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(root / 'capture.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile({
            "id": "fixture", "name": "Fixture Profile", "email": "fixture@example.invalid",
            "facts": [],
        })
        self.store.save_profile({
            "id": "other", "name": "Other Profile", "email": "other@example.invalid",
            "facts": [],
        })
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "employer", "name": "Fixture Employer", "domain": "employer.example",
            })
            upsert(connection, m.jobs, {
                "id": "job-1", "company_id": "employer", "requisition": "ROLE-1",
                "title": "Systems Administrator", "description": "Synthetic fixture role.",
                "skills": [], "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _capture(self, *, source_url, application_url, request_id="capture-1", profile_id="fixture"):
        with self.store.engine.begin() as connection:
            return record_supported_collection(
                connection,
                profile_id=profile_id,
                request_id=request_id,
                job_id="job-1",
                source_url=source_url,
                application_url=application_url,
            )

    def test_same_domain_job_is_not_queued_as_a_new_source(self):
        result = self._capture(
            source_url="https://www.employer.example/jobs.json",
            application_url="https://employer.example/careers/role-1",
        )

        self.assertIsNone(result)
        self.assertEqual(list_candidates(self.store, "fixture")["candidates"], [])

    def test_cross_domain_result_is_sanitized_truthfully_scoped_and_replay_safe(self):
        args = {
            "source_url": "https://employer.example/jobs.json?token=source-secret",
            "application_url": "https://jobs.example-ats.invalid/apply/1?session=private-secret#form",
            "request_id": "supported-role-1",
        }
        candidate_id = self._capture(**args)
        replayed_id = self._capture(**args)

        candidates = list_candidates(self.store, "fixture")["candidates"]
        other_profile_candidates = list_candidates(self.store, "other")["candidates"]
        self.assertEqual(candidate_id, replayed_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(other_profile_candidates, [])
        candidate = candidates[0]
        self.assertEqual(candidate["id"], candidate_id)
        self.assertEqual(candidate["domain"], "jobs.example-ats.invalid")
        self.assertEqual(candidate["state"], "NEEDS_REVIEW")
        self.assertFalse(candidate["collection_enabled"])
        self.assertEqual(len(candidate["observations"]), 1)
        observation = candidate["observations"][0]
        self.assertEqual(observation["job_id"], "job-1")
        self.assertEqual(observation["capture_method"], "SUPPORTED_COLLECTION")
        self.assertIsNone(observation["application_id"])
        self.assertIsNone(observation["discovery_origin"])
        self.assertEqual(observation["origin_attribution"], "UNKNOWN")
        self.assertEqual(observation["evidence_status"], "PARTIAL")
        self.assertEqual(observation["source_url"], "https://employer.example/jobs.json")
        self.assertEqual(observation["application_url"], "https://jobs.example-ats.invalid/apply/1")
        self.assertEqual(observation["observed_hops"], [{"state": "UNKNOWN"}])
        self.assertNotIn("source-secret", str(candidate))
        self.assertNotIn("private-secret", str(candidate))

    def test_malformed_or_unknown_urls_do_not_create_false_attribution(self):
        malformed = self._capture(
            source_url="javascript:prompt('x')",
            application_url="https://jobs.example-ats.invalid/apply/1",
            request_id="malformed-source",
        )
        unknown = self._capture(
            source_url="https://employer.example/jobs.json",
            application_url=None,
            request_id="unknown-destination",
        )

        self.assertIsNone(malformed)
        self.assertIsNone(unknown)
        self.assertEqual(list_candidates(self.store, "fixture")["candidates"], [])


if __name__ == "__main__":
    unittest.main()
