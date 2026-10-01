import unittest

from services.discovery.search_semantics import (
    classify_location,
    evaluate_search,
    filter_jobs,
    normalize_employment_type,
)


class SearchSemanticsTests(unittest.TestCase):
    def test_remote_classification_preserves_restrictions(self):
        result = classify_location(
            "Remote - United States",
            "Applicants must reside in TX, OK, AR, KS.",
        )
        self.assertEqual(result["remote_type"], "REMOTE_SPECIFIC_STATES")
        self.assertEqual(result["remote_regions"], ["AR", "KS", "OK", "TX"])
        self.assertEqual(result["country"], "US")
        self.assertTrue(result["remote_indicated"])

    def test_remote_worldwide_and_hybrid_are_distinct(self):
        self.assertEqual(classify_location("Remote Worldwide")["remote_type"], "REMOTE_WORLDWIDE")
        self.assertEqual(classify_location("Hybrid - Austin, TX")["remote_type"], "HYBRID")
        self.assertEqual(classify_location("Austin, TX", "On-site role")["remote_type"], "ONSITE")

    def test_employment_type_normalization(self):
        cases = {
            "Permanent full-time employee": "FULL_TIME",
            "Part-time support technician": "PART_TIME",
            "6 month contract-to-hire": "CONTRACT_TO_HIRE",
            "1099 contractor": "CONTRACT",
            "Temporary assignment": "TEMPORARY",
            "Summer internship": "INTERNSHIP",
            "No employment type stated": "UNKNOWN",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalize_employment_type(text), expected)

    def test_structured_and_or_not_semantics_and_aliases(self):
        criteria = {
            "include_titles": ["System Administrator", "Infrastructure"],
            "exclude_titles": ["Senior"],
            "required_keywords": ["Microsoft 365", "PowerShell"],
            "optional_keywords": ["Azure", "Intune"],
            "excluded_keywords": ["commission"],
            "exact_phrases": ["Active Directory"],
            "technology_aliases": {"Microsoft 365": ["M365", "Office 365"]},
            "company_include": ["Acme"],
            "company_exclude": ["Staffing"],
            "remote": "REQUIRED",
            "work_arrangements": ["REMOTE"],
            "countries": ["US"],
            "states": ["TX"],
            "remote_regions": ["TX"],
            "employment_types": ["FULL_TIME"],
        }
        job = {
            "title": "Infrastructure System Administrator",
            "company": "Acme Systems",
            "description": "Manage M365, Active Directory, PowerShell and Azure.",
            "skills": ["PowerShell"],
            "location": "Remote - TX",
            "remote_type": "REMOTE_SPECIFIC_STATES",
            "attributes": {
                "remote_indicated": True,
                "remote_regions": ["TX", "OK"],
                "country": "US",
                "state": "TX",
                "city": None,
                "employment_type": "FULL_TIME",
            },
        }
        result = evaluate_search(criteria, job)
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["optional_matches"], ["Azure"])

        blocked = dict(job, title="Senior Infrastructure System Administrator")
        result = evaluate_search(criteria, blocked)
        self.assertFalse(result["matched"])
        self.assertIn("TITLE_EXCLUDED", result["reasons"])

    def test_radius_filter_uses_coordinates_only_when_available(self):
        criteria = {
            "radius_miles": 50.0,
            "radius_latitude": 33.9137,
            "radius_longitude": -96.4111,
        }
        nearby = {
            "title": "Role", "company": "Example", "description": "", "skills": [],
            "location": "Sherman, TX", "remote_type": "ONSITE",
            "attributes": {"coordinates": {"latitude": 33.6357, "longitude": -96.6089}},
        }
        far = {
            **nearby,
            "attributes": {"coordinates": {"latitude": 32.7767, "longitude": -96.7970}},
        }
        self.assertTrue(evaluate_search(criteria, nearby)["matched"])
        self.assertIn("OUTSIDE_RADIUS", evaluate_search(criteria, far)["reasons"])
        missing = {**nearby, "attributes": {}}
        self.assertIn("RADIUS_DATA_UNAVAILABLE", evaluate_search(criteria, missing)["reasons"])

    def test_filter_jobs_returns_search_evidence(self):
        rows = [
            {"title": "System Administrator", "company": "A", "description": "Linux", "skills": [],
             "location": "Remote", "remote_type": "REMOTE_US", "attributes": {"remote_indicated": True}},
            {"title": "Sales Director", "company": "B", "description": "Linux", "skills": [],
             "location": "Remote", "remote_type": "REMOTE_US", "attributes": {"remote_indicated": True}},
        ]
        result = filter_jobs(rows, {"include_titles": ["System Administrator"]})
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["search_evidence"]["matched"])


if __name__ == "__main__":
    unittest.main()
