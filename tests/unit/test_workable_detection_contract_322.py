import unittest

from services.ats_detection.detector import detect_ats


class WorkableDetectionContractTests(unittest.TestCase):
    def test_declared_workable_script_host_is_positive_evidence(self):
        result = detect_ats(
            "https://careers.acme.example/jobs",
            '<script src="https://jobs.workable.com/acme/widget.js"></script>',
        )

        self.assertEqual(result.status, "MATCH")
        self.assertEqual(result.provider, "workable")
        self.assertEqual(result.evidence, ("SCRIPT_URL",))
        self.assertFalse(result.application_supported)

    def test_declared_workable_public_api_script_pattern_is_positive_evidence(self):
        for url in (
            "https://workable.com/api/jobs",
            "https://workable.com/api/widget",
        ):
            with self.subTest(url=url):
                result = detect_ats(
                    "https://careers.acme.example/jobs",
                    f'<script src="{url}"></script>',
                )

                self.assertEqual(result.status, "MATCH")
                self.assertEqual(result.provider, "workable")
                self.assertEqual(result.evidence, ("PUBLIC_API", "SCRIPT_URL"))
                self.assertFalse(result.application_supported)

    def test_deceptive_or_malformed_workable_script_urls_are_not_evidence(self):
        for url in (
            "https://attacker.example/assets/jobs.workable.com/widget.js",
            "https://attacker.example/widget.js?source=jobs.workable.com",
            "https://jobs.workable.com.attacker.example/widget.js",
            "https://jobs.workable.com@attacker.example/widget.js",
            "https://jobs.workable.com:invalid/widget.js",
            "javascript://jobs.workable.com/widget.js",
        ):
            with self.subTest(url=url):
                result = detect_ats(
                    "https://careers.acme.example/jobs",
                    f'<script src="{url}"></script>',
                )

                self.assertEqual(result.status, "NO_MATCH")
                self.assertIsNone(result.provider)
                self.assertEqual(result.candidates, ())
                self.assertFalse(result.application_supported)

    def test_workable_and_lever_evidence_remain_ambiguous(self):
        result = detect_ats(
            "https://apply.workable.com/acme",
            '<script src="https://jobs.lever.co/acme/widget.js"></script>',
        )

        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertIsNone(result.provider)
        self.assertEqual(
            {candidate.provider for candidate in result.candidates},
            {"workable", "lever"},
        )
        self.assertFalse(result.application_supported)


if __name__ == "__main__":
    unittest.main()
