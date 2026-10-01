import unittest

from services.discovery.search_semantics import (
    classify_location,
    evaluate_search,
    filter_jobs,
    normalize_employment_type,
    normalize_experience_level,
    normalize_title,
    parse_education_requirement,
    parse_salary,
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

    def test_experience_level_normalization_preserves_original_phrase(self):
        cases = {
            "Senior Systems Administrator": ("SENIOR", "Senior"),
            "Lead Infrastructure Engineer": ("LEAD", "Lead"),
            "Entry-Level Support Analyst": ("ENTRY", "Entry-Level"),
            "Director of Cloud Operations": ("DIRECTOR", "Director"),
            "Platform Engineer": ("UNKNOWN", None),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                result = normalize_experience_level(title)
                self.assertEqual((result["level"], result["raw"]), expected)

    def test_salary_parser_handles_required_forms_and_provenance(self):
        cases = [
            ("$60,000–$75,000 annually", 60000, 75000, "YEAR"),
            ("$32–38/hour", 32, 38, "HOUR"),
            ("$70k+", 70000, None, "UNKNOWN"),
            ("Up to $95,000 per year", None, 95000, "YEAR"),
            ("Starting at $65k", 65000, None, "UNKNOWN"),
            ("$30/hr", 30, 30, "HOUR"),
        ]
        for text, low, high, period in cases:
            with self.subTest(text=text):
                result = parse_salary(text, "EMPLOYER_PROVIDED")
                self.assertEqual((result["salary_min"], result["salary_max"]), (low, high))
                self.assertEqual(result["salary_period"], period)
                self.assertEqual(result["salary_source"], "EMPLOYER_PROVIDED")
                self.assertIsNotNone(result["salary_raw"])

    def test_experience_salary_and_date_filters_are_deterministic(self):
        job = {
            "title": "Senior Platform Engineer", "company": "Example", "description": "", "skills": [],
            "location": "Remote US", "remote_type": "REMOTE_US",
            "attributes": {
                "experience_level": "SENIOR",
                "salary_min": 120000.0, "salary_max": 150000.0,
                "salary_period": "YEAR", "salary_source": "EMPLOYER_PROVIDED",
                "reported_posted_at": "2026-10-01T16:30:00+00:00",
            },
        }
        criteria = {
            "experience_levels": ["SENIOR", "LEAD"],
            "salary_min": 100000.0, "salary_max": 160000.0, "salary_period": "YEAR",
            "salary_known_only": True, "date_window": "LAST_HOUR",
        }
        self.assertTrue(evaluate_search(criteria, job, at="2026-10-01T17:00:00+00:00")["matched"])
        too_low = {**job, "attributes": {**job["attributes"], "salary_max": 90000.0}}
        self.assertIn("SALARY_BELOW_MINIMUM", evaluate_search(criteria, too_low, at="2026-10-01T17:00:00+00:00")["reasons"])
        old = {**job, "attributes": {**job["attributes"], "reported_posted_at": "2026-09-30T12:00:00+00:00"}}
        self.assertIn("DATE_OUTSIDE_WINDOW", evaluate_search(criteria, old, at="2026-10-01T17:00:00+00:00")["reasons"])

    def test_date_filter_falls_back_to_first_seen_without_fabricating_posted_time(self):
        job = {
            "title": "Role", "company": "Example", "description": "", "skills": [],
            "location": "Unknown", "remote_type": "UNKNOWN",
            "attributes": {"reported_posted_at": None, "first_seen_at": "2026-10-01T16:45:00+00:00"},
        }
        result = evaluate_search({"date_window": "LAST_HOUR"}, job, at="2026-10-01T17:00:00+00:00")
        self.assertTrue(result["matched"], result["reasons"])

    def test_title_normalization_is_conservative_and_preserves_original(self):
        examples = [
            "Sys Admin", "System Admin", "Systems Administrator", "Windows Sysadmin",
        ]
        for title in examples:
            with self.subTest(title=title):
                result = normalize_title(title)
                self.assertEqual(result["title_family"], "SYSTEMS_ADMINISTRATION")
                self.assertEqual(result["normalized_title"], "Systems Administrator")
                self.assertEqual(result["original_title"], title)
        database = normalize_title("Database Administrator")
        self.assertIsNone(database["title_family"])
        self.assertEqual(database["normalized_title"], "Database Administrator")

    def test_education_requirement_categories_remain_distinct(self):
        cases = {
            "Bachelor's degree required.": "BACHELORS_REQUIRED",
            "Bachelor's degree preferred.": "BACHELORS_PREFERRED",
            "Bachelor's degree or equivalent experience required.": "BACHELORS_OR_EQUIVALENT_EXPERIENCE",
            "College degree preferred.": "DEGREE_PREFERRED",
            "Three years of operations experience.": "NONE_SPECIFIED",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_education_requirement(text)["classification"], expected)

    def test_normalized_skill_filters_are_optional_and_explicit(self):
        job = {
            "title": "Systems Administrator", "company": "Example", "description": "", "skills": ["Active Directory", "Rubrik"],
            "location": "Remote", "remote_type": "REMOTE_US",
            "attributes": {"skill_ids": ["ACTIVE_DIRECTORY", "RUBRIK"]},
        }
        self.assertTrue(evaluate_search({}, job)["matched"])
        result = evaluate_search({
            "required_skills": ["ACTIVE_DIRECTORY"],
            "optional_skills": ["RUBRIK", "AZURE"],
            "excluded_skills": ["AWS"],
        }, job)
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["optional_skill_matches"], ["RUBRIK"])
        missing = evaluate_search({"required_skills": ["AZURE"]}, job)
        self.assertIn("REQUIRED_SKILL_MISSING:AZURE", missing["reasons"])
        excluded = evaluate_search({"excluded_skills": ["RUBRIK"]}, job)
        self.assertIn("EXCLUDED_SKILL:RUBRIK", excluded["reasons"])


if __name__ == "__main__":
    unittest.main()
