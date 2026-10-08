import unittest

from services.discovery.requirement_parsers import (
    experience_requirement_summary,
    parse_certifications,
    parse_clearance,
    parse_job_requirements,
    parse_skill_requirements,
    parse_work_authorization,
)
from services.discovery.skill_taxonomy import extract_skill_mentions
from services.discovery.search_semantics import evaluate_search


class RequirementParserTests(unittest.TestCase):
    def test_skill_strengths_require_local_unambiguous_classification(self):
        description = (
            "Windows Server required. Active Directory preferred. "
            "PowerShell is a nice-to-have. Docker is a must-have. Linux."
        )
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual(
            {row["id"]: row["strength"] for row in rows},
            {"WINDOWS_SERVER": "REQUIRED", "ACTIVE_DIRECTORY": "PREFERRED",
             "POWERSHELL": "NICE_TO_HAVE", "DOCKER": "REQUIRED", "LINUX": "UNKNOWN"},
        )

    def test_skill_strength_negation_conflict_and_ambiguous_lists_fail_closed(self):
        description = (
            "Windows Server is not required. Active Directory required but preferred. "
            "PowerShell, Linux required. Docker preferred, but Docker is a bonus. "
            "Azure mustn't be required."
        )
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual(
            {row["id"]: row["strength"] for row in rows},
            {"WINDOWS_SERVER": "UNKNOWN", "ACTIVE_DIRECTORY": "UNKNOWN",
             "POWERSHELL": "UNKNOWN", "LINUX": "UNKNOWN", "DOCKER": "UNKNOWN",
             "AZURE": "UNKNOWN"},
        )

    def test_same_skill_required_and_negated_required_in_one_clause_is_unknown(self):
        description = "Windows Server is required, but Windows Server is not required."
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual({row["id"]: row["strength"] for row in rows},
                         {"WINDOWS_SERVER": "UNKNOWN"})

    def test_explicit_and_negated_skills_keep_distinct_strengths_in_one_clause(self):
        description = "Windows Server required, Active Directory is not required."
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual(
            {row["id"]: row["strength"] for row in rows},
            {"WINDOWS_SERVER": "REQUIRED", "ACTIVE_DIRECTORY": "UNKNOWN"},
        )

    def test_skill_aliases_keep_canonical_identity_and_strength_scope(self):
        description = "AD required. Azure AD preferred."
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual(
            [(row["id"], row["skill"], row["strength"]) for row in rows],
            [("ACTIVE_DIRECTORY", "Active Directory", "REQUIRED"),
             ("ENTRA_ID", "Entra ID", "PREFERRED")],
        )

    def test_skill_strength_does_not_cross_clause_boundaries(self):
        description = "Windows Server required; Active Directory is not required."
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual(
            {row["id"]: row["strength"] for row in rows},
            {"WINDOWS_SERVER": "REQUIRED", "ACTIVE_DIRECTORY": "UNKNOWN"},
        )

    def test_qualification_section_headings_scope_unqualified_skill_bullets(self):
        description = (
            "Required Qualifications:\nWindows Server\nActive Directory\n\n"
            "Preferred Qualifications:\nPowerShell\nPython"
        )
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual(
            {row["id"]: row["strength"] for row in rows},
            {"WINDOWS_SERVER": "REQUIRED", "ACTIVE_DIRECTORY": "REQUIRED",
             "POWERSHELL": "PREFERRED", "PYTHON": "PREFERRED"},
        )

    def test_qualification_heading_scope_stops_at_next_heading_and_unknown_heading(self):
        description = (
            "Required Qualifications\nWindows Server\n"
            "Preferred Qualifications\nPowerShell\n"
            "Additional Information\nPython"
        )
        rows = parse_skill_requirements(description, extract_skill_mentions(description))
        self.assertEqual(
            {row["id"]: row["strength"] for row in rows},
            {"WINDOWS_SERVER": "REQUIRED", "POWERSHELL": "PREFERRED",
             "PYTHON": "UNKNOWN"},
        )

    def test_heading_inference_fails_closed_for_unlabelled_conflicting_and_inline_text(self):
        descriptions = (
            ("Windows Server\nPowerShell", {"WINDOWS_SERVER": "UNKNOWN", "POWERSHELL": "UNKNOWN"}),
            ("Required Qualifications:\nWindows Server is preferred", {"WINDOWS_SERVER": "UNKNOWN"}),
            ("The role includes a Required Qualifications heading and Windows Server.",
             {"WINDOWS_SERVER": "UNKNOWN"}),
        )
        for description, expected in descriptions:
            with self.subTest(description=description):
                rows = parse_skill_requirements(description, extract_skill_mentions(description))
                self.assertEqual({row["id"]: row["strength"] for row in rows}, expected)

    def test_individually_qualified_comma_list_stays_per_skill(self):
        for description, expected in (
            ("Windows Server required, Active Directory preferred.",
             {"WINDOWS_SERVER": "REQUIRED", "ACTIVE_DIRECTORY": "PREFERRED"}),
            ("Active Directory preferred, Windows Server required.",
             {"ACTIVE_DIRECTORY": "PREFERRED", "WINDOWS_SERVER": "REQUIRED"}),
        ):
            with self.subTest(description=description):
                rows = parse_skill_requirements(description, extract_skill_mentions(description))
                self.assertEqual({row["id"]: row["strength"] for row in rows}, expected)

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

    def test_unqualified_certification_is_unknown_and_explicit_strengths_are_preserved(self):
        rows = parse_certifications(
            "CCNA. Security+ required. AZ-104 preferred. ITIL is a bonus."
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["CCNA"]["strength"], "UNKNOWN")
        self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "REQUIRED")
        self.assertEqual(by_id["AZ_104"]["strength"], "PREFERRED")
        self.assertEqual(by_id["ITIL"]["strength"], "NICE_TO_HAVE")

    def test_negated_required_words_do_not_override_unknown_preferred_or_nice_to_have(self):
        rows = parse_certifications(
            "CCNA is not required. AZ-104 is not required but preferred. "
            "Security+ is mandatory. ITIL is not required but a bonus. "
            "MCSA is not mandatory, just nice to have."
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["CCNA"]["strength"], "UNKNOWN")
        self.assertEqual(by_id["AZ_104"]["strength"], "PREFERRED")
        self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "REQUIRED")
        self.assertEqual(by_id["ITIL"]["strength"], "NICE_TO_HAVE")
        self.assertEqual(by_id["MCSA"]["strength"], "NICE_TO_HAVE")

    def test_compound_negated_required_or_mandatory_clause_stays_unknown(self):
        rows = parse_certifications(
            "CCNA is not required or mandatory. Security+ is not mandatory."
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["CCNA"]["strength"], "UNKNOWN")
        self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "UNKNOWN")

    def test_negated_adverb_and_article_before_required_or_mandatory_stay_unknown(self):
        rows = parse_certifications(
            "CCNA is not a mandatory certification. AZ-104 is not necessarily required."
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["CCNA"]["strength"], "UNKNOWN")
        self.assertEqual(by_id["AZ_104"]["strength"], "UNKNOWN")

    def test_no_longer_and_contracted_required_negations_stay_unknown(self):
        rows = parse_certifications("CCNA is no longer required. Security+ isn't required.")
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["CCNA"]["strength"], "UNKNOWN")
        self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "UNKNOWN")

    def test_negated_must_have_does_not_become_required(self):
        rows = parse_certifications("CCNA is not a must-have.")
        self.assertEqual(rows[0]["strength"], "UNKNOWN")

    def test_no_certification_required_phrasings_are_unknown_without_changing_positive(self):
        rows = parse_certifications(
            "CCNA required. No CCNA required. No Security+ certification is required."
        )
        ccna = [row["strength"] for row in rows if row["id"] == "CCNA"]
        security_plus = [row for row in rows if row["id"] == "COMPTIA_SECURITY_PLUS"]
        self.assertEqual(ccna, ["REQUIRED", "UNKNOWN"])
        self.assertEqual([row["strength"] for row in security_plus], ["UNKNOWN"])

    def test_comma_separated_certification_strengths_are_scoped_per_mention(self):
        for description in (
            "Security+ required, but CCNA preferred.",
            "CCNA preferred, but Security+ required.",
        ):
            with self.subTest(description=description):
                rows = parse_certifications(description)
                by_id = {row["id"]: row for row in rows}
                self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "REQUIRED")
                self.assertEqual(by_id["CCNA"]["strength"], "PREFERRED")

    def test_comma_separated_certification_strengths_without_but_are_scoped(self):
        for description in (
            "Security+ required, CCNA preferred.",
            "CCNA preferred, Security+ required.",
        ):
            with self.subTest(description=description):
                rows = parse_certifications(description)
                by_id = {row["id"]: row for row in rows}
                self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "REQUIRED")
                self.assertEqual(by_id["CCNA"]["strength"], "PREFERRED")

        ambiguous = parse_certifications("CCNA, Security+ required.")
        by_id = {row["id"]: row for row in ambiguous}
        self.assertEqual(by_id["CCNA"]["strength"], "UNKNOWN")
        self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "REQUIRED")

    def test_and_connected_certification_strengths_are_scoped_in_both_orders(self):
        for description in (
            "Security+ required, and CCNA preferred.",
            "CCNA preferred, and Security+ required.",
            "Security+ required and CCNA preferred.",
            "CCNA preferred and Security+ required.",
        ):
            with self.subTest(description=description):
                rows = parse_certifications(description)
                by_id = {row["id"]: row for row in rows}
                self.assertEqual(by_id["COMPTIA_SECURITY_PLUS"]["strength"], "REQUIRED")
                self.assertEqual(by_id["CCNA"]["strength"], "PREFERRED")

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
