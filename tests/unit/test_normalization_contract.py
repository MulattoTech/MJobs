"""Cross-field contract tests for provider-neutral job normalization."""

import unittest
from copy import deepcopy

from services.discovery.requirement_parsers import parse_clearance
from services.discovery.search_semantics import (
    classify_location,
    normalize_employment_type,
    normalize_experience_level,
    normalize_title,
    parse_education_requirement,
    parse_salary,
    parse_timestamp,
)
from services.discovery.skill_taxonomy import (
    canonical_skill_id,
    extract_skills,
    normalize_skill_list,
)
from services.normalization.jobs import (
    canonical_job_view,
    normalize_company_name,
    raw_posting_view,
)


CANONICAL_KEYS = {
    "canonical_job_id",
    "title",
    "normalized_title",
    "title_family",
    "company_id",
    "company_name",
    "normalized_company_name",
    "description_html",
    "description_text",
    "responsibilities",
    "required_qualifications",
    "preferred_qualifications",
    "skills",
    "employment_type",
    "experience_level",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_source",
    "location_text",
    "city",
    "state",
    "country",
    "remote_type",
    "remote_regions",
    "reported_posted_at",
    "first_seen_at",
    "last_seen_at",
    "expires_at",
    "status",
    "lifecycle",
}


class NormalizationContractTests(unittest.TestCase):
    def _canonical_row(self, **provider_fields):
        return {
            "id": "job-214",
            "company_id": "company-214",
            "company": "Example Systems, Inc.",
            "title": "Sr. Sys Admin",
            "description": "Administer M365 and PowerShell.",
            "skills": ["MICROSOFT_365", "POWERSHELL"],
            "location": "Remote - United States",
            "remote_type": "REMOTE_SPECIFIC_STATES",
            "attributes": {
                "normalized_title": "Systems Administrator",
                "title_family": "SYSTEMS_ADMINISTRATION",
                "employment_type": "FULL_TIME",
                "experience_level": "SENIOR",
                "salary_min": 90000.0,
                "salary_max": 120000.0,
                "salary_currency": "USD",
                "salary_period": "YEAR",
                "salary_source": "EMPLOYER_PROVIDED",
                "city": None,
                "state": None,
                "country": "US",
                "remote_regions": ["OK", "TX"],
                "reported_posted_at": "2026-10-20T12:00:00+00:00",
                "first_seen_at": "2026-10-20T13:00:00+00:00",
                "last_seen_at": "2026-10-21T13:00:00+00:00",
                "expires_at": None,
                "status": "ACTIVE",
                "lifecycle": "OPEN",
                "education_requirement": "BACHELORS_OR_EQUIVALENT_EXPERIENCE",
                "education_requirement_raw": "Bachelor's degree or equivalent experience required",
                "clearance": {"level": "NONE_STATED", "raw": None, "clause": None},
            },
            **provider_fields,
        }

    def test_canonical_shape_is_provider_neutral_and_preserves_requirement_fields(self):
        greenhouse = canonical_job_view(self._canonical_row(
            provider="greenhouse",
            source_job_id="greenhouse-17",
        ))
        lever = canonical_job_view(self._canonical_row(
            provider="lever",
            source_job_id="lever-92",
        ))

        self.assertEqual(
            {key: greenhouse[key] for key in CANONICAL_KEYS},
            {key: lever[key] for key in CANONICAL_KEYS},
        )
        self.assertTrue(CANONICAL_KEYS.issubset(greenhouse))
        self.assertEqual(greenhouse["attributes"]["education_requirement"],
                         "BACHELORS_OR_EQUIVALENT_EXPERIENCE")
        self.assertEqual(greenhouse["attributes"]["clearance"]["level"], "NONE_STATED")

    def test_title_normalization_is_conservative_and_retains_original_text(self):
        normalized = normalize_title("  Sr.   Windows Sysadmin  ")
        self.assertEqual(normalized, {
            "original_title": "Sr. Windows Sysadmin",
            "normalized_title": "Systems Administrator",
            "title_family": "SYSTEMS_ADMINISTRATION",
        })
        unrelated = normalize_title("Database Administrator")
        self.assertEqual(unrelated, {
            "original_title": "Database Administrator",
            "normalized_title": "Database Administrator",
            "title_family": None,
        })

    def test_company_spelling_normalization_never_infers_alias_identity(self):
        self.assertEqual(normalize_company_name(" Example  Systems, Incorporated "),
                         "example systems")
        self.assertEqual(normalize_company_name("Example Systems LLC"), "example systems")
        self.assertNotEqual(normalize_company_name("Google LLC"),
                            normalize_company_name("Alphabet Inc."))
        self.assertNotEqual(normalize_company_name("Acme Medical"),
                            normalize_company_name("Acme Staffing"))

    def test_location_normalization_keeps_remote_restrictions_explicit(self):
        result = classify_location(
            "Remote - United States",
            "Applicants must reside in TX, OK, AR, KS.",
        )
        self.assertEqual(result, {
            "remote_type": "REMOTE_SPECIFIC_STATES",
            "remote_regions": ["AR", "KS", "OK", "TX"],
            "remote_indicated": True,
            "country": "US",
            "state": "AR",
            "city": None,
            "location_text": "Remote - United States",
        })
        withheld = classify_location("Location withheld")
        self.assertEqual(withheld["remote_type"], "UNKNOWN")
        self.assertEqual(withheld["remote_regions"], [])
        self.assertIsNone(withheld["country"])
        self.assertIsNone(withheld["state"])

    def test_salary_normalization_preserves_source_text_and_rejects_malformed_values(self):
        result = parse_salary("$90,000-$120,000 annually", "EMPLOYER_PROVIDED")
        self.assertEqual(result, {
            "salary_min": 90000.0,
            "salary_max": 120000.0,
            "salary_period": "YEAR",
            "currency": "USD",
            "salary_source": "EMPLOYER_PROVIDED",
            "salary_raw": "$90,000-$120,000",
        })
        self.assertEqual(parse_salary("compensation not disclosed"), {
            "salary_min": None,
            "salary_max": None,
            "salary_period": "UNKNOWN",
            "currency": None,
            "salary_source": "UNKNOWN",
            "salary_raw": None,
        })
        with self.assertRaisesRegex(ValueError, "Salary source is invalid"):
            parse_salary("$90,000", "PROVIDER_GUESSED")

    def test_date_normalization_is_utc_deterministic_and_never_uses_implicit_now(self):
        absolute = parse_timestamp("2026-10-20T08:30:00-04:00")
        relative = parse_timestamp(
            "2 hours ago",
            reference_at="2026-10-20T14:30:00+00:00",
        )
        self.assertEqual(absolute.isoformat(), "2026-10-20T12:30:00+00:00")
        self.assertEqual(relative.isoformat(), "2026-10-20T12:30:00+00:00")
        self.assertIsNone(parse_timestamp("2 hours ago"))
        self.assertIsNone(parse_timestamp("not-a-date"))
        self.assertIsNone(parse_timestamp(""))

    def test_employment_and_experience_normalization_have_explicit_unknowns(self):
        self.assertEqual(normalize_employment_type("6 month contract-to-hire"),
                         "CONTRACT_TO_HIRE")
        self.assertEqual(normalize_employment_type("schedule not disclosed"), "UNKNOWN")
        self.assertEqual(normalize_employment_type(None), "UNKNOWN")

        self.assertEqual(normalize_experience_level("Senior Platform Engineer"), {
            "level": "SENIOR",
            "raw": "Senior",
        })
        self.assertEqual(normalize_experience_level("Platform Engineer"), {
            "level": "UNKNOWN",
            "raw": None,
        })
        self.assertEqual(normalize_experience_level(None), {
            "level": "UNKNOWN",
            "raw": None,
        })

    def test_skill_normalization_retains_mentions_and_rejects_unknown_labels(self):
        result = extract_skills("Administer M365 and PowerShell; do not infer COBOL.")
        self.assertEqual(result["ids"], ["MICROSOFT_365", "POWERSHELL"])
        self.assertEqual([row["original"] for row in result["mentions"]],
                         ["M365", "PowerShell"])
        self.assertEqual(normalize_skill_list(["Office 365", "MICROSOFT_365", "PowerShell"]),
                         ["MICROSOFT_365", "POWERSHELL"])
        self.assertIsNone(canonical_skill_id("Imaginary Platform"))
        self.assertEqual(extract_skills(None), {"ids": [], "labels": [], "mentions": []})
        with self.assertRaisesRegex(ValueError, "Unknown normalized skill"):
            normalize_skill_list(["Imaginary Platform"])

    def test_education_normalization_keeps_strength_evidence_and_unknowns(self):
        text = "Bachelor's degree or equivalent experience required."
        result = parse_education_requirement(text)
        self.assertEqual(result["classification"], "BACHELORS_OR_EQUIVALENT_EXPERIENCE")
        self.assertEqual(result["raw"], text.rstrip("."))
        for unknown in (None, "", "Three years of operations experience.", {"bad": "shape"}):
            with self.subTest(unknown=unknown):
                self.assertEqual(parse_education_requirement(unknown), {
                    "classification": "NONE_SPECIFIED",
                    "raw": None,
                })

    def test_clearance_normalization_keeps_evidence_without_inferring_user_identity(self):
        result = parse_clearance("Active TS/SCI clearance required.")
        self.assertEqual(result["level"], "TS_SCI")
        self.assertEqual(result["raw"], "TS/SCI")
        self.assertEqual(result["clause"], "Active TS/SCI clearance required")
        for unknown in (None, "", "Normal commercial role.", {"bad": "shape"}):
            with self.subTest(unknown=unknown):
                self.assertEqual(parse_clearance(unknown), {
                    "level": "NONE_STATED",
                    "raw": None,
                    "clause": None,
                })

    def test_empty_canonical_input_uses_only_explicit_unknown_sentinels(self):
        result = canonical_job_view({})
        self.assertTrue(CANONICAL_KEYS.issubset(result))
        self.assertIsNone(result["canonical_job_id"])
        self.assertIsNone(result["title"])
        self.assertIsNone(result["normalized_title"])
        self.assertIsNone(result["company_name"])
        self.assertEqual(result["normalized_company_name"], "")
        self.assertEqual(result["description_text"], "")
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["responsibilities"], [])
        self.assertEqual(result["required_qualifications"], [])
        self.assertEqual(result["preferred_qualifications"], [])
        self.assertEqual(result["location_text"], "Unknown")
        self.assertIsNone(result["city"])
        self.assertIsNone(result["state"])
        self.assertIsNone(result["country"])
        self.assertEqual(result["remote_regions"], [])
        self.assertEqual(result["remote_type"], "UNKNOWN")
        self.assertEqual(result["employment_type"], "UNKNOWN")
        self.assertEqual(result["experience_level"], "UNKNOWN")
        self.assertIsNone(result["salary_min"])
        self.assertIsNone(result["salary_max"])
        self.assertIsNone(result["salary_currency"])
        self.assertEqual(result["salary_period"], "UNKNOWN")
        self.assertEqual(result["salary_source"], "UNKNOWN")
        self.assertIsNone(result["reported_posted_at"])

    def test_raw_and_canonical_views_do_not_mutate_or_cross_fill_source_values(self):
        canonical_input = self._canonical_row()
        canonical_before = deepcopy(canonical_input)
        canonical = canonical_job_view(canonical_input)
        self.assertEqual(canonical_input, canonical_before)
        self.assertNotIn("raw_structured_payload", canonical)
        self.assertNotIn("fetch_timestamp", canonical)

        provider_payload = {"id": 17, "salary": "not disclosed"}
        posting = {
            "id": "posting-214",
            "job_id": "job-214",
            "source": "https://feed.example.test/jobs",
            "source_job_id": "17",
            "raw": provider_payload,
            "fetched_at": "2026-10-20T13:00:00+00:00",
        }
        posting_before = deepcopy(posting)
        raw = raw_posting_view(posting)
        self.assertEqual(posting, posting_before)
        self.assertIs(raw["raw_structured_payload"], provider_payload)
        self.assertEqual(raw["fetch_timestamp"], posting["fetched_at"])
        self.assertEqual(raw["source_url"], posting["source"])
        self.assertEqual(raw["application_url"], "")
        self.assertNotIn("normalized_title", raw)
        self.assertNotIn("salary_min", raw)


if __name__ == "__main__":
    unittest.main()
