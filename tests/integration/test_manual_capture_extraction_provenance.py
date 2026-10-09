"""POST /jobs/manual durably retains only safe, reviewed extraction evidence."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity


def _capture_payload():
    fields = {
        "title": "Systems Administrator",
        "company": "Example Employer",
        "description": "Maintain Linux systems.",
        "location": "Remote",
        "employment_type": None,
        "date_posted": None,
        "valid_through": None,
        "source_job_id": None,
        "listing_url": "https://jobs.example.invalid/listing/7?token=private-listing",
        "application_url": "https://jobs.example.invalid/apply/7?session=private-apply",
    }
    sources = {
        "title": ("USER_SELECTED_TEXT", 0.82, False, True),
        "company": ("JOBPOSTING_SCHEMA", 0.98, False, False),
        "description": ("USER_SELECTED_TEXT", 0.82, False, True),
        "location": ("VISIBLE_TEXT", 0.65, True, True),
        "employment_type": ("UNKNOWN", 0.0, False, False),
        "date_posted": ("UNKNOWN", 0.0, False, False),
        "valid_through": ("UNKNOWN", 0.0, False, False),
        "source_job_id": ("UNKNOWN", 0.0, False, False),
        "listing_url": ("PROVIDER_FIELDS", 0.9, False, True),
        "application_url": ("VISIBLE_TEXT", 0.65, True, True),
    }
    extraction_capture = {
        "schema_version": 1,
        "raw_fields": fields,
        "field_provenance": {
            field: {
                "source": source,
                "confidence": confidence,
                "review_required": review_required,
                "user_corrected": corrected,
            }
            for field, (source, confidence, review_required, corrected) in sources.items()
        },
        "raw_capture_ref": "sha256:integration-fixture-7",
        "review_receipt": {
            "schema_version": 1,
            "confirmed_by_user": True,
            "reviewed_fields": ["title", "company", "description", "location", "listing_url", "application_url"],
            "rejected_fields": [],
        },
    }
    return {
        "profile_id": "fixture",
        "request_id": "manual-extraction-api-7",
        "title": "Senior Systems Administrator",
        "company": "Example Employer",
        "description": "Maintain Linux systems and support production services.",
        "location": "Oklahoma, Remote",
        "reference_url": "https://www.linkedin.com/jobs/view/7?token=private-source",
        "application_url": "https://jobs.example.invalid/apply/confirmed-7?session=private-route#form",
        "discovery_origin": "LinkedIn",
        "observed_hops": [
            "https://redirect.example.invalid/continue?session=private-hop",
        ],
        "source_details": {
            "extraction_capture": extraction_capture,
        },
    }


class ManualCaptureExtractionProvenanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-manual-extraction-api-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'capture.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile({
            "id": "fixture", "name": "Fixture Profile", "email": "fixture@example.invalid",
            "facts": [],
        })
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_manual_post_readback_preserves_confirmed_values_and_sanitized_provenance(self):
        payload = _capture_payload()
        first = self.client.post("/jobs/manual", json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        replay = self.client.post("/jobs/manual", json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertFalse(replay.json()["created"])

        posting_id = identity("manual-posting", payload["request_id"])
        with self.store.engine.connect() as connection:
            raw = connection.execute(select(m.postings.c.raw).where(
                m.postings.c.id == posting_id
            )).scalar_one()
        persisted = raw["submitted_fields"]
        self.assertEqual(persisted["title"], "Senior Systems Administrator")
        self.assertEqual(persisted["location"], "Oklahoma, Remote")
        extraction = persisted["source_details"]["extraction_capture"]
        self.assertEqual(extraction["review_receipt"], {
            "schema_version": 1,
            "confirmed_by_user": True,
            "reviewed_fields": ["title", "company", "description", "location", "listing_url", "application_url"],
            "rejected_fields": [],
        })
        self.assertEqual(extraction["raw_fields"]["title"], "Systems Administrator")
        self.assertEqual(extraction["field_provenance"]["title"]["user_corrected"], True)
        self.assertEqual(extraction["field_provenance"]["location"]["source"], "VISIBLE_TEXT")
        self.assertEqual(extraction["field_provenance"]["location"]["review_required"], True)
        self.assertEqual(extraction["field_provenance"]["date_posted"]["source"], "UNKNOWN")
        self.assertEqual(extraction["field_provenance"]["date_posted"]["confidence"], 0.0)
        self.assertEqual(
            extraction["raw_fields"]["listing_url"],
            "https://jobs.example.invalid/listing/7",
        )
        self.assertEqual(
            persisted["application_url"],
            "https://jobs.example.invalid/apply/confirmed-7",
        )
        self.assertEqual(
            persisted["observed_hops"],
            ["https://redirect.example.invalid/continue"],
        )
        self.assertNotIn("private-source", str(raw))
        self.assertNotIn("private-route", str(raw))
        self.assertNotIn("private-hop", str(raw))
        self.assertNotIn("private-listing", str(raw))
        self.assertNotIn("private-apply", str(raw))

        candidate = self.client.get(
            "/source-candidates", params={"profile_id": "fixture"}).json()["candidates"][0]
        self.assertEqual(candidate["observations"][0]["discovery_origin"], "LinkedIn")
        self.assertEqual(candidate["observations"][0]["origin_attribution"], "USER_REPORTED")
        self.assertEqual(candidate["observations"][0]["observed_hops"], [
            "https://redirect.example.invalid/continue",
        ])
        self.assertFalse(candidate["collection_enabled"])
        self.assertEqual(self.store.count("job_postings"), 1)
        self.assertEqual(self.store.count("source_candidate_observations"), 1)


if __name__ == "__main__":
    unittest.main()
