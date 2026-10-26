"""MJ-REQ-217: sustain evidence-based ATS detection without network access."""
import unittest

from services.ats_detection.detector import detect_ats


class ATSDetectionSustain218Tests(unittest.TestCase):
    def assert_match(self, result, provider, evidence):
        self.assertEqual(result.status, "MATCH")
        self.assertEqual(result.provider, provider)
        self.assertIn(evidence, result.evidence)
        self.assertFalse(result.application_supported)

    def test_direct_host_branded_domain_redirect_and_embedded_ats(self):
        cases = (
            (
                "direct ATS host",
                detect_ats("https://jobs.lever.co/acme"),
                "lever",
                "HOSTNAME",
            ),
            (
                "custom branded domain",
                detect_ats(
                    "https://careers.acme.example/jobs",
                    '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
                ),
                "greenhouse",
                "SCRIPT_URL",
            ),
            (
                "redirect evidence",
                detect_ats(
                    "https://careers.acme.example/jobs",
                    redirect_chain=(
                        "https://acme.wd5.myworkdayjobs.com/External/job/US/Analyst_R123",
                    ),
                ),
                "workday",
                "REDIRECT_HOSTNAME",
            ),
            (
                "embedded ATS",
                detect_ats(
                    "https://careers.acme.example/jobs",
                    '<iframe src="https://job-boards.greenhouse.io/acme"></iframe>',
                ),
                "greenhouse",
                "BOARD_LINK",
            ),
        )

        for scenario, result, provider, evidence in cases:
            with self.subTest(scenario=scenario):
                self.assert_match(result, provider, evidence)
                self.assertEqual(result.visible_careers_url,
                                 "https://jobs.lever.co/acme" if scenario == "direct ATS host"
                                 else "https://careers.acme.example/jobs")

    def test_unknown_and_ambiguous_portals_fail_closed(self):
        unknown = detect_ats(
            "https://careers.acme.example/jobs",
            "<main>Open roles at Acme</main>",
        )
        ambiguous = detect_ats(
            "https://jobs.lever.co/acme",
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
        )

        self.assertEqual(unknown.status, "NO_MATCH")
        self.assertIsNone(unknown.provider)
        self.assertEqual(unknown.confidence, 0.0)
        self.assertEqual(unknown.evidence, ())
        self.assertEqual(unknown.candidates, ())
        self.assertFalse(unknown.application_supported)

        self.assertEqual(ambiguous.status, "AMBIGUOUS")
        self.assertIsNone(ambiguous.provider)
        self.assertEqual(
            {candidate.provider for candidate in ambiguous.candidates},
            {"greenhouse", "lever"},
        )
        self.assertEqual(ambiguous.evidence, ())
        self.assertFalse(ambiguous.application_supported)

    def test_spoofed_hosts_and_missing_structural_evidence_fail_closed(self):
        cases = (
            (
                "direct lookalike host",
                detect_ats("https://jobs.lever.co.attacker.example/acme"),
            ),
            (
                "redirect lookalike host",
                detect_ats(
                    "https://careers.acme.example/jobs",
                    redirect_chain=("https://boards.greenhouse.io.attacker.example/acme",),
                ),
            ),
            (
                "script lookalike host",
                detect_ats(
                    "https://careers.acme.example/jobs",
                    '<script src="https://attacker.example/assets/jobs.lever.co/widget.js"></script>',
                ),
            ),
            (
                "vendor prose without structural evidence",
                detect_ats(
                    "https://careers.acme.example/jobs",
                    "<main>Greenhouse, Lever, and Workday experience preferred.</main>",
                ),
            ),
            (
                "generic portal path without evidence",
                detect_ats("https://careers.acme.example/recruiting/"),
            ),
        )

        for scenario, result in cases:
            with self.subTest(scenario=scenario):
                self.assertEqual(result.status, "NO_MATCH")
                self.assertIsNone(result.provider)
                self.assertEqual(result.confidence, 0.0)
                self.assertEqual(result.evidence, ())
                self.assertEqual(result.candidates, ())
                self.assertFalse(result.application_supported)


if __name__ == "__main__":
    unittest.main()
