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

    def test_remote_classification_uses_geographic_statements_instead_of_code_like_words(self):
        us = classify_location("Remote", "Work in the United States. We support IT and AI operations.")
        self.assertEqual((us["remote_type"], us["remote_regions"]), ("REMOTE_US", ["US"]))
        us_only = classify_location("Remote", "Work from anywhere in the US.")
        self.assertEqual((us_only["remote_type"], us_only["remote_regions"]), ("REMOTE_US", ["US"]))
        states = classify_location("Remote", "Fully remote if you reside in one of the following states: Oklahoma, Texas.")
        self.assertEqual((states["remote_type"], states["remote_regions"]),
                         ("REMOTE_SPECIFIC_STATES", ["OK", "TX"]))

    def test_remote_residence_requires_supported_eligibility(self):
        criteria = {"remote": "REQUIRED", "remote_residence_state": "OK"}
        cases = (
            ("Remote, United States", "", True, None),
            ("Remote, United States", "Open anywhere in the United States except Oklahoma.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, United States", "Oklahoma residents are not eligible.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Applicants based in OK may not apply.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Not available to residents of Oklahoma.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Applicants in OK aren't eligible.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "This role is not open to Oklahoma residents.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "We are unable to hire in Oklahoma.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "No hiring in Oklahoma.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Applicants residing in TX and AR can apply.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Candidates residing in OK or TX may apply.", True, None),
            ("Remote, US", "Applicants must reside in Texas.", False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Applicants must reside in the United States.", True, None),
            ("Remote, US", "Applicants must reside in the United States, specifically in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the United States. This role supports Linux.", True, None),
            ("Remote, US", "Applicants must reside in the United States. Candidates should have Linux experience.", True, None),
            ("Remote, US", "Applicants must reside in the United States, specifically in California. Linux support experience required.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the U.S.", True, None),
            ("Remote, US", "Applicants must reside in the US.", True, None),
            ("Remote, US", "Applicants must reside in the U.S., specifically in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the U.S. This role supports Linux.", True, None),
            ("Remote, US", "Applicants must reside in the US, specifically in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the US. This role supports Linux.", True, None),
            ("Remote, US", "Applicants must reside in the U.S. only in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the U.S. (California only).",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote", "Applicants must reside in the U.S., specifically in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote", "Applicants must reside in the U.S. only in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the U.S. only. Oklahoma residents are not eligible.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Applicants must reside in the United States. This role supports Linux. "
             "The role is remote within the United States, specifically in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the u.s., specifically in California.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the United States or Canada.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the US approved states.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the United States. Only Pacific time zone applicants are eligible.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Remote within the US. Only applicants in the Pacific time zone may apply.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in the United States. Collaboration hours overlap the Pacific time zone.",
             True, None),
            ("Remote, US", "Applicants must reside in OK, TX.", True, None),
            ("Remote, US", "Applicants must reside in Oklahoma or Texas.", True, None),
            ("Remote", "Fully remote options if you reside in one of the following states: Oklahoma, Texas.",
             True, None),
            ("Remote, US", "Fully remote options if you reside in one of the following states: Texas, Arizona.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "We cannot hire in AK, OK, or TX.", False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Applicants must reside in Canada.", False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote Canada", "", False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote", "Work from anywhere.", False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote", "Manage worldwide infrastructure.", False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote", "Our company is based in the United States.", False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote", "Applicants must reside in one of our approved states.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, US", "Applicants must reside in one of our approved states.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, Arizona", "", False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, Arizona", "Applicants must reside in Oklahoma or Texas.",
             False, "REMOTE_RESIDENCE_EXCLUDED"),
            ("Remote, US", "Candidates must be based in Oklahoma City.",
             False, "REMOTE_RESIDENCE_UNKNOWN"),
            ("Remote, Oklahoma", "", True, None),
            ("Remote Worldwide", "", True, None),
        )
        for location, description, matched, reason in cases:
            with self.subTest(location=location, description=description):
                attributes = classify_location(location, description)
                job = {"location": location, "description": description, "attributes": attributes,
                       "remote_type": attributes["remote_type"]}
                result = evaluate_search(criteria, job)
                self.assertEqual(result["matched"], matched, result["reasons"])
                if reason:
                    self.assertIn(reason, result["reasons"])
                else:
                    self.assertEqual(result["remote_residence"], {"state": "OK", "status": "ELIGIBLE"})

    def test_remote_scope_fallback_requires_unqualified_us_geography(self):
        criteria = {"remote": "REQUIRED", "remote_residence_state": "OK"}
        rows = (
            ("United States", "This role is remote within the {country}", True, "ELIGIBLE"),
            ("US", "This role is remote within the {country}", True, "ELIGIBLE"),
            ("U.S.", "This role is remote within the {country}", True, "ELIGIBLE"),
            ("United States", "This role is remote within the {country}. This role supports Linux.", True, "ELIGIBLE"),
            ("US", "This role is remote within the {country}. This role supports Linux.", True, "ELIGIBLE"),
            ("U.S.", "This role is remote within the {country}. This role supports Linux.", True, "ELIGIBLE"),
            ("United States", "This role is remote within the {country}, specifically in California.", False, "UNKNOWN"),
            ("US", "This role is remote within the {country}, specifically in California.", False, "UNKNOWN"),
            ("U.S.", "This role is remote within the {country}, specifically in California.", False, "UNKNOWN"),
            ("U.S.", "This role is remote within the {country} only in California.", False, "UNKNOWN"),
            ("U.S.", "This role is remote within the {country} (California only).", False, "UNKNOWN"),
            ("u.s.", "This role is remote within the {country}, specifically in California.", False, "UNKNOWN"),
            ("United States", "This role is remote within the {country}. California residents only.", False, "UNKNOWN"),
            ("United States", "This role is remote within the {country} or Canada.", False, "UNKNOWN"),
            ("United States", "This role is remote within the {country} approved states.", False, "UNKNOWN"),
            ("United States", "This role is remote within the {country} only.", True, "ELIGIBLE"),
            ("U.S.", "This role is remote within the {country} only.", True, "ELIGIBLE"),
            ("U.S.", "This role is remote within the {country} only. This role supports Linux.", True, "ELIGIBLE"),
            ("United States", "This role is remote within the {country} only. This role supports Linux.", True, "ELIGIBLE"),
        )
        for country, template, matched, status in rows:
            description = template.format(country=country)
            description = description.replace("U.S.. ", "U.S. ")
            for location in ("Remote", "Remote, US"):
                with self.subTest(location=location, description=description):
                    attributes = classify_location(location, description)
                    result = evaluate_search(criteria, {"location": location, "description": description,
                                                       "attributes": attributes, "remote_type": attributes["remote_type"]})
                    self.assertEqual(result["remote_residence"]["status"], status)
                    self.assertEqual(result["matched"], matched, result["reasons"])

    def test_technical_hybrid_cloud_does_not_override_remote_arrangement(self):
        technical_phrases = ("Maintain our hybrid cloud infrastructure.",
                             "Maintain our hybrid model for cloud deployment.")
        criteria = {"remote": "REQUIRED", "remote_residence_state": "OK"}
        for description in technical_phrases:
            with self.subTest(description=description):
                attributes = classify_location("Remote, United States", description)
                self.assertEqual(attributes["remote_type"], "REMOTE_US")
                job = {"location": "Remote, United States", "description": description,
                       "attributes": attributes, "remote_type": attributes["remote_type"]}
                self.assertTrue(evaluate_search(criteria, job)["matched"])
        description = "Hybrid work schedule with three remote days."
        attributes = classify_location("Remote, United States", description)
        self.assertEqual(attributes["remote_type"], "HYBRID")
        self.assertFalse(evaluate_search(criteria, {
            "location": "Remote, United States", "description": description,
            "attributes": attributes, "remote_type": attributes["remote_type"],
        })["matched"])

    def test_hybrid_with_remote_days_is_kept_in_cross_border_local_search(self):
        description = "Two remote days and three days in the office."
        for location in ("Durant, OK", "Denison, TX", "Sherman, TX"):
            with self.subTest(location=location):
                attributes = classify_location(location, "Hybrid. " + description)
                job = {"location": location, "description": description, "attributes": attributes,
                       "remote_type": attributes["remote_type"]}
                self.assertFalse(evaluate_search({"remote": "REQUIRED"}, job)["matched"])
                local_criteria = {"work_arrangements": ["HYBRID", "ONSITE"],
                                  "states": ["OK", "TX"], "cities": ["Durant", "Denison", "Sherman"]}
                self.assertTrue(evaluate_search(local_criteria, job)["matched"])

    def test_occasional_required_onsite_work_is_not_fully_remote(self):
        for description in ("The position requires occasional on-site work.",
                            "This role requires 3 days per week on-site.",
                            "Occasional on-site visits may be required."):
            with self.subTest(description=description):
                attributes = classify_location("Remote, United States", description)
                self.assertEqual(attributes["remote_type"], "HYBRID")
                legacy_job = {"location": "Remote, United States", "description": description,
                              "remote_type": "REMOTE_US", "attributes": {"remote_indicated": True}}
                self.assertFalse(evaluate_search({"remote": "REQUIRED"}, legacy_job)["matched"])
        optional = classify_location("Remote, United States", "The role does not require on-site work.")
        self.assertEqual(optional["remote_type"], "REMOTE_US")

    def test_texoma_search_includes_captured_and_older_local_jobs(self):
        criteria = {"work_arrangements": ["HYBRID", "ONSITE"], "states": ["OK", "TX"],
                    "cities": ["Durant", "Denison", "Sherman"]}
        cases = (
            ("Durant, OK", {"source_type": "MANUAL_USER_ENTRY"}, True),
            ("Denison, TX", {"state": "IN", "city": "Denison"}, True),
            ("Denison, IA", {"state": "TX", "city": "Denison"}, False),
            ("Sherman", {}, False),
        )
        for location, attributes, expected in cases:
            with self.subTest(location=location, attributes=attributes):
                job = {"location": location, "remote_type": "ONSITE", "attributes": attributes}
                self.assertEqual(evaluate_search(criteria, job)["matched"], expected)

    def test_location_state_accepts_only_known_arrangement_suffixes(self):
        criteria = {"work_arrangements": ["HYBRID", "ONSITE"], "states": ["OK", "TX"],
                    "cities": ["Durant", "Denison", "Sherman"]}
        for location, expected_city, expected_state, expected_arrangement in (
            ("Durant, OK (Hybrid)", "Durant", "OK", "HYBRID"),
            ("Durant, Oklahoma (Hybrid)", "Durant", "OK", "HYBRID"),
            ("Denison, TX (On-site)", "Denison", "TX", "ONSITE"),
            ("Durant, OK (Hiring)", "Durant", None, "UNKNOWN"),
        ):
            with self.subTest(location=location):
                attributes = classify_location(location)
                self.assertEqual(attributes["city"], expected_city)
                self.assertEqual(attributes["state"], expected_state)
                self.assertEqual(attributes["remote_type"], expected_arrangement)
                job = {"location": location, "attributes": attributes,
                       "remote_type": attributes["remote_type"]}
                self.assertEqual(evaluate_search(criteria, job)["matched"], expected_state is not None)

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
