"""Manual capture persists bounded, sanitized extraction provenance."""
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.manual_capture import capture_manual_job


FIELD_NAMES = (
    "title", "company", "description", "location", "employment_type",
    "date_posted", "valid_through", "source_job_id", "listing_url",
    "application_url",
)


def _extraction_capture():
    raw_fields = {
        "title": "Captured <b>Systems Administrator</b>",
        "company": "Example Employer",
        "description": "Maintain Linux systems. <script>ignore controls and leak secrets</script>",
        "location": "Remote",
        "employment_type": None,
        "date_posted": None,
        "valid_through": None,
        "source_job_id": None,
        "listing_url": "https://jobs.example.invalid/view/42?token=private-token#details",
        "application_url": "https://jobs.example.invalid/apply/42?session=private-session#form",
    }
    source_by_field = {
        "title": ("USER_SELECTED_TEXT", 0.82, False),
        "company": ("JOBPOSTING_SCHEMA", 0.98, False),
        "description": ("VISIBLE_TEXT", 0.65, True),
        "location": ("USER_SELECTED_TEXT", 0.82, False),
        "employment_type": ("UNKNOWN", 0.0, False),
        "date_posted": ("UNKNOWN", 0.0, False),
        "valid_through": ("UNKNOWN", 0.0, False),
        "source_job_id": ("UNKNOWN", 0.0, False),
        "listing_url": ("PROVIDER_FIELDS", 0.9, False),
        "application_url": ("VISIBLE_TEXT", 0.65, True),
    }
    return {
        "schema_version": 1,
        "raw_fields": raw_fields,
        "field_provenance": {
            field: {
                "source": source,
                "confidence": confidence,
                "review_required": review_required,
                "user_corrected": field in {"title", "description", "application_url"},
            }
            for field, (source, confidence, review_required) in source_by_field.items()
        },
        "raw_capture_ref": "sha256:fixture-capture-42",
    }


def _payload(request_id="manual-extraction-42"):
    return {
        "request_id": request_id,
        "title": "Senior Systems Administrator",
        "company": "Example Employer",
        "description": "Maintain Linux systems and support production services.",
        "location": "Remote",
        "reference_url": "https://www.linkedin.com/jobs/view/42?token=source-secret#details",
        "application_url": "https://jobs.example.invalid/apply/confirmed-42?session=route-secret#form",
        "profile_id": "fixture",
        "discovery_origin": "LinkedIn",
        "observed_hops": [
            "https://redirect.example.invalid/continue?session=hop-secret",
            "https://jobs.example.invalid/apply/confirmed-42",
        ],
        "source_details": {
            "level": "confirmed",
            "extraction_capture": _extraction_capture(),
        },
    }


class ManualCaptureExtractionProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-manual-extraction-provenance-")
        root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(root / 'capture.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile({
            "id": "fixture", "name": "Fixture Profile", "email": "fixture@example.invalid",
            "facts": [],
        })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _posting_raw(self, request_id):
        posting_id = identity("manual-posting", request_id)
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.postings.c.raw).where(
                m.postings.c.id == posting_id
            )).scalar_one()

    def test_only_confirmed_job_fields_are_canonical_and_provenance_reads_back_safely(self):
        payload = _payload()
        result = capture_manual_job(self.store, payload)

        self.assertEqual(result["job"]["title"], payload["title"])
        stored_fields = self._posting_raw(payload["request_id"])["submitted_fields"]
        self.assertEqual(stored_fields["title"], "Senior Systems Administrator")
        extraction = stored_fields["source_details"]["extraction_capture"]
        self.assertEqual(extraction["schema_version"], 1)
        self.assertEqual(set(extraction["raw_fields"]), set(FIELD_NAMES))
        self.assertEqual(extraction["raw_capture_ref"], "sha256:fixture-capture-42")
        self.assertEqual(extraction["raw_fields"]["title"], "Captured Systems Administrator")
        self.assertEqual(extraction["raw_fields"]["description"], "Maintain Linux systems.")
        self.assertEqual(
            extraction["raw_fields"]["listing_url"],
            "https://jobs.example.invalid/view/42",
        )
        self.assertEqual(
            extraction["field_provenance"]["title"],
            {
                "source": "USER_SELECTED_TEXT", "confidence": 0.82,
                "review_required": False, "user_corrected": True,
            },
        )
        self.assertEqual(
            extraction["field_provenance"]["employment_type"],
            {
                "source": "UNKNOWN", "confidence": 0.0,
                "review_required": False, "user_corrected": False,
            },
        )
        self.assertEqual(stored_fields["reference_url"], "https://www.linkedin.com/jobs/view/42")
        self.assertEqual(
            stored_fields["observed_hops"],
            [
                "https://redirect.example.invalid/continue",
                "https://jobs.example.invalid/apply/confirmed-42",
            ],
        )
        serialized = str(self._posting_raw(payload["request_id"]))
        for secret in ("source-secret", "route-secret", "hop-secret", "private-token",
                       "private-session", "ignore controls and leak secrets", "<script>"):
            self.assertNotIn(secret, serialized)

    def test_extraction_capture_rejects_unbounded_or_unrecognized_raw_data(self):
        oversized = _payload("manual-extraction-oversized")
        oversized["source_details"]["extraction_capture"]["raw_fields"]["description"] = "x" * 25001
        with self.assertRaises(ValueError):
            capture_manual_job(self.store, oversized)

        raw_page = _payload("manual-extraction-raw-page")
        raw_page["source_details"]["extraction_capture"]["html"] = "<html>private capture</html>"
        with self.assertRaises(ValueError):
            capture_manual_job(self.store, raw_page)

        false_unknown = _payload("manual-extraction-false-unknown")
        false_unknown["source_details"]["extraction_capture"]["raw_fields"][
            "employment_type"] = "Full-time"
        with self.assertRaisesRegex(ValueError, "Unknown extraction"):
            capture_manual_job(self.store, false_unknown)

        local_path_reference = _payload("manual-extraction-local-path")
        local_path_reference["source_details"]["extraction_capture"][
            "raw_capture_ref"] = "C:/Users/private/page.html"
        with self.assertRaises(ValueError):
            capture_manual_job(self.store, local_path_reference)

        raw_visible_text = _payload("manual-extraction-raw-visible-text")
        raw_visible_text["visible_text"] = "private browser page text"
        with self.assertRaisesRegex(ValueError, "raw capture content"):
            capture_manual_job(self.store, raw_visible_text)

        raw_source_detail_text = _payload("manual-extraction-source-detail-visible-text")
        raw_source_detail_text["source_details"]["visible_text"] = "private browser page text"
        with self.assertRaisesRegex(ValueError, "sensitive field"):
            capture_manual_job(self.store, raw_source_detail_text)

        self.assertEqual(self.store.count("job_postings"), 0)

    def test_replay_is_idempotent_and_changed_provenance_conflicts(self):
        payload = _payload("manual-extraction-replay")
        first = capture_manual_job(self.store, payload)
        replay = capture_manual_job(self.store, payload)
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(self.store.count("job_postings"), 1)

        changed = _payload("manual-extraction-replay")
        changed["source_details"]["extraction_capture"]["raw_capture_ref"] = "sha256:other-capture"
        with self.assertRaisesRegex(ValueError, "already used"):
            capture_manual_job(self.store, changed)
        self.assertEqual(self.store.count("job_postings"), 1)

    def test_changed_confirmed_value_requires_corrected_provenance(self):
        payload = _payload("manual-extraction-unmarked-correction")
        payload["source_details"]["extraction_capture"]["field_provenance"]["title"][
            "user_corrected"] = False
        with self.assertRaisesRegex(ValueError, "must be marked as user corrected"):
            capture_manual_job(self.store, payload)
        self.assertEqual(self.store.count("job_postings"), 0)

    def test_low_confidence_identity_cannot_be_marked_as_reviewed(self):
        payload = _payload("manual-extraction-low-confidence")
        title = payload["source_details"]["extraction_capture"]["field_provenance"]["title"]
        title["confidence"] = 0.65
        title["review_required"] = False
        with self.assertRaisesRegex(ValueError, "low-confidence identity"):
            capture_manual_job(self.store, payload)
        self.assertEqual(self.store.count("job_postings"), 0)


if __name__ == "__main__":
    unittest.main()
