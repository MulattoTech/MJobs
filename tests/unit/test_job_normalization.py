import unittest

from services.normalization.jobs import canonical_job_view, normalize_company_name, raw_posting_view


class JobNormalizationTests(unittest.TestCase):
    def test_canonical_view_has_stable_provider_neutral_shape(self):
        row = {
            "id": "job-1",
            "company_id": "company-1",
            "company": "Acme Corporation",
            "title": "Windows Sysadmin",
            "description": "Manage infrastructure",
            "skills": ["Windows", "PowerShell"],
            "location": "Remote - United States",
            "remote_type": "REMOTE_US",
            "attributes": {
                "normalized_title": "Systems Administrator",
                "title_family": "SYSTEMS_ADMINISTRATION",
                "employment_type": "FULL_TIME",
                "experience_level": "MID",
                "salary_min": 90000,
                "salary_max": 120000,
                "salary_currency": "USD",
                "salary_period": "YEAR",
                "salary_source": "EMPLOYER_PROVIDED",
                "country": "US",
                "remote_regions": ["US"],
                "first_seen_at": "2026-10-01T00:00:00+00:00",
                "last_seen_at": "2026-10-01T01:00:00+00:00",
            },
        }
        result = canonical_job_view(row)
        self.assertEqual(result["canonical_job_id"], "job-1")
        self.assertEqual(result["normalized_title"], "Systems Administrator")
        self.assertEqual(result["normalized_company_name"], "acme")
        self.assertEqual(result["description_text"], "Manage infrastructure")
        self.assertEqual(result["salary_min"], 90000)
        self.assertEqual(result["remote_regions"], ["US"])
        self.assertEqual(result["id"], "job-1")  # compatibility
    def test_company_normalization_does_not_assert_identity(self):
        self.assertEqual(normalize_company_name("Acme, Inc."), "acme")
        self.assertEqual(normalize_company_name("Acme Corporation"), "acme")
        self.assertEqual(normalize_company_name("Acme Medical"), "acme medical")

    def test_raw_posting_view_keeps_payload_and_urls_separate(self):
        raw = {"id": 123, "content": "<p>Original HTML</p>"}
        row = {
            "id": "posting-1",
            "job_id": "job-1",
            "source": "https://source.test/feed",
            "source_job_id": "123",
            "source_url": "https://source.test/feed",
            "application_url": "https://ats.test/apply/123",
            "raw": raw,
            "fetched_at": "2026-10-01T00:00:00+00:00",
        }
        result = raw_posting_view(row)
        self.assertIs(result["raw"], raw)
        self.assertEqual(result["raw_structured_payload"], raw)
        self.assertEqual(result["fetch_timestamp"], row["fetched_at"])
        self.assertEqual(result["source_url"], row["source_url"])
        self.assertEqual(result["application_url"], row["application_url"])


if __name__ == "__main__":
    unittest.main()
