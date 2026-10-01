import unittest

from services.discovery.requirement_parsers import (
    experience_requirement_summary,
    parse_certifications,
    parse_clearance,
    parse_job_requirements,
    parse_work_authorization,
)
from services.discovery.search_semantics import evaluate_search


class RequirementParserTests(unittest.TestCase):
    def test_experience_requirements_preserve_bounds_strength_and_equivalence(self):
        result = experience_requirement_summary(
            "Requires 3-5 years of systems administration. "
            "5 years preferred for enterprise environments. Equivalent experience accepted."
        )
        self.assertEqual(result["minimum_years"], 3)
        self.assertEqual(result["maximum_years"], 5)
        self.assertEqual(result["preferred_years"], 5)
        self.assertTrue(result["equivalent_experience_accepted"])
        self.assertEqual(result["requirements"][0]["requirement_strength"], "REQUIRED")
        self.assertEqual(result["requirements"][1]["requirement_strength"], "PREFERRED")

    def test_plus_years_has_open_upper_bound(self):
        result = experience_requirement_summary("Minimum 2+ years required.")
        self.assertEqual(result["minimum_years"], 2)
        self.assertIsNone(result["maximum_years"])
    def test_certifications_are_classified_by_local_clause(self):
        rows = parse_certifications(
            "CompTIA Security+ is required. CCNA preferred. ITIL Foundation is a nice to have."
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "REQUIRED")
        self.assertEqual(by_id["CCNA"]["strength"], "PREFERRED")
        self.assertEqual(by_id["ITIL"]["strength"], "NICE_TO_HAVE")
        self.assertEqual(by_id["ITIL"]["raw"], "ITIL Foundation")

    def test_clearance_distinguishes_levels_and_none_stated(self):
        self.assertEqual(parse_clearance("Active TS/SCI clearance required.")["level"], "TS_SCI")
        self.assertEqual(parse_clearance("Ability to obtain a Public Trust.")["level"], "PUBLIC_TRUST")
        self.assertEqual(parse_clearance("Normal commercial role.")["level"], "NONE_STATED")

    def test_work_authorization_extracts_statements_without_user_inference(self):
        parsed = parse_work_authorization(
            "U.S. citizenship required. No visa sponsorship. Must already be authorized to work in the United States."
        )
        self.assertEqual(
            set(parsed["flags"]),
            {"US_CITIZENSHIP_REQUIRED", "NO_SPONSORSHIP", "ALREADY_AUTHORIZED_REQUIRED"},
        )
        self.assertEqual(len(parsed["statements"]), 3)

    def test_job_requirement_bundle_keeps_raw_evidence(self):
        result = parse_job_requirements(
            "2+ years required. AZ-104 preferred. Secret clearance required. Sponsorship available."
        )
        self.assertEqual(result["experience"]["minimum_years"], 2)
        self.assertEqual(result["certifications"][0]["id"], "AZ_104")
        self.assertEqual(result["clearance"]["level"], "SECRET")
        self.assertIn("SPONSORSHIP_AVAILABLE", result["work_authorization"]["flags"])

    def test_search_policy_can_exclude_clearance_and_work_authorization_flags(self):
        job = {
            "title": "Infrastructure Engineer",
            "company": "Example",
            "description": "",
            "skills": [],
            "location": "Remote",
            "remote_type": "REMOTE_US",
            "attributes": {
                "clearance": {"level": "SECRET", "raw": "Secret"},
                "work_authorization": {"flags": ["NO_SPONSORSHIP"], "statements": []},
            },
        }
        result = evaluate_search({
            "exclude_clearance_required": True,
            "excluded_work_authorization_flags": ["NO_SPONSORSHIP"],
        }, job)
        self.assertFalse(result["matched"])
        self.assertIn("CLEARANCE_REQUIRED", result["reasons"])
        self.assertIn("WORK_AUTHORIZATION_EXCLUDED:NO_SPONSORSHIP", result["reasons"])

        allowed = evaluate_search({
            "clearance_levels": ["SECRET"],
            "excluded_work_authorization_flags": [],
        }, job)
        self.assertTrue(allowed["matched"], allowed["reasons"])


if __name__ == "__main__":
    unittest.main()
