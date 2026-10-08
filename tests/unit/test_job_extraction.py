"""Deterministic extraction from already supplied local capture data."""
import unittest

from services.applications.job_extraction import extract_job_capture


class JobExtractionTests(unittest.TestCase):
    def extract(self, capture):
        return extract_job_capture(capture)

    def test_supported_jobposting_schema_wins_conflicts_and_keeps_only_reference(self):
        result = self.extract({
            "raw_capture_ref": "capture-opaque-17",
            "job_posting": {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Schema Systems Administrator",
                "description": "Manage Linux systems.",
                "hiringOrganization": {"name": "Schema Employer"},
                "jobLocation": {"address": {"addressLocality": "Tulsa", "addressRegion": "OK"}},
                "employmentType": "FULL_TIME",
                "identifier": {"value": "schema-42"},
                "url": "https://jobs.example.test/schema-42",
                "applicationUrl": "https://apply.example.test/schema-42",
                "datePosted": "2026-10-01",
                "validThrough": "2026-12-01",
            },
            "provider": "greenhouse",
            "provider_fields": {
                "title": "Provider Conflicting Title",
                "company_name": "Provider Employer",
                "id": "provider-99",
            },
            "selected_text": "Title: Selected Conflicting Title\nCompany: Selected Employer",
            "visible_text": "Title: Visible Conflicting Title\nemail: private@example.test",
        })

        self.assertEqual(result["raw_capture_ref"], "capture-opaque-17")
        self.assertEqual(result["fields"]["title"], {
            "value": "Schema Systems Administrator", "source": "JOBPOSTING_SCHEMA",
            "confidence": 0.98, "review_required": False,
        })
        self.assertEqual(result["fields"]["company"]["value"], "Schema Employer")
        self.assertEqual(result["fields"]["location"]["value"], "Tulsa, OK")
        self.assertEqual(result["fields"]["source_job_id"]["value"], "schema-42")
        self.assertEqual(result["fields"]["listing_url"]["value"],
                         "https://jobs.example.test/schema-42")
        self.assertEqual(result["fields"]["application_url"]["value"],
                         "https://apply.example.test/schema-42")
        self.assertNotIn("private@example.test", repr(result))
        self.assertNotIn("provider_fields", result)

    def test_malformed_schema_falls_through_to_provider_specific_fields(self):
        result = self.extract({
            "job_posting": '{"@type":"JobPosting","title":["not","text"]',
            "provider": "greenhouse",
            "provider_fields": {
                "id": "greenhouse-7",
                "title": "Provider Systems Engineer",
                "company_name": "Greenhouse Employer",
                "content": "<p>Support Linux services.</p>",
                "location": {"name": "Remote"},
                "absolute_url": "https://boards.example.test/jobs/7",
                "application_url": "https://boards.example.test/jobs/7#app",
            },
            "selected_text": "Title: Lower Priority Selected Role",
            "visible_text": "Title: Lowest Priority Visible Role",
        })

        self.assertEqual(result["fields"]["title"]["value"], "Provider Systems Engineer")
        self.assertEqual(result["fields"]["title"]["source"], "PROVIDER_FIELDS")
        self.assertEqual(result["fields"]["description"]["value"], "Support Linux services.")
        self.assertEqual(result["fields"]["location"]["value"], "Remote")
        self.assertEqual(result["fields"]["source_job_id"]["value"], "greenhouse-7")

    def test_user_selected_text_precedes_visible_text(self):
        result = self.extract({
            "selected_text": (
                "Title: Confirmed User Role\nCompany: Selected Employer\n"
                "Description: The selected job description."
            ),
            "visible_text": (
                "Title: Stale Visible Role\nCompany: Stale Employer\n"
                "Description: The surrounding page text."
            ),
        })

        self.assertEqual(result["fields"]["title"]["value"], "Confirmed User Role")
        self.assertEqual(result["fields"]["title"]["source"], "USER_SELECTED_TEXT")
        self.assertEqual(result["fields"]["title"]["confidence"], 0.82)
        self.assertFalse(result["fields"]["title"]["review_required"])
        self.assertEqual(result["fields"]["company"]["value"], "Selected Employer")
        self.assertEqual(result["fields"]["description"]["value"],
                         "The selected job description.")

    def test_low_confidence_visible_identity_and_route_are_marked_for_review(self):
        result = self.extract({
            "visible_text": (
                "Title: Possible Systems Role\nCompany: Possible Employer\n"
                "Application URL: https://apply.example.test/unknown"
            ),
        })

        self.assertEqual(result["review_required"], ["title", "company", "application_url"])
        for field in result["review_required"]:
            self.assertTrue(result["fields"][field]["review_required"])
            self.assertEqual(result["fields"][field]["source"], "VISIBLE_TEXT")

    def test_unknown_fields_are_explicit_and_capture_reference_does_not_expand(self):
        result = self.extract({"raw_capture_ref": "capture-ref-1", "visible_text": ""})

        self.assertEqual(result["raw_capture_ref"], "capture-ref-1")
        self.assertEqual(set(result["fields"]), {
            "title", "company", "description", "location", "employment_type",
            "date_posted", "valid_through", "source_job_id", "listing_url",
            "application_url",
        })
        self.assertTrue(all(value == {
            "value": None, "source": "UNKNOWN", "confidence": 0.0,
            "review_required": False,
        } for value in result["fields"].values()))
        self.assertEqual(result["review_required"], [])
        self.assertEqual(set(result), {"fields", "raw_capture_ref", "review_required"})

    def test_visible_text_fallback_is_bounded(self):
        result = self.extract({
            "visible_text": "Possible Role\n" + ("x" * 20_000) + "\nTitle: Hidden Tail Role",
        })

        self.assertEqual(result["fields"]["title"]["value"], "Possible Role")
        self.assertEqual(result["fields"]["title"]["source"], "VISIBLE_TEXT")
        self.assertNotIn("Hidden Tail Role", repr(result))

    def test_bad_capture_container_is_rejected(self):
        with self.assertRaises(ValueError):
            self.extract("not-a-capture-mapping")


if __name__ == "__main__":
    unittest.main()
