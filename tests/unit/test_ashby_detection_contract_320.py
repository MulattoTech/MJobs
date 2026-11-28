"""Ashby-specific ATS detection contract coverage for issue #320."""

import unittest

from services.ats_detection.detector import detect_ats


class AshbyDetectionContractTests(unittest.TestCase):
    def test_vendor_script_is_evidence_but_deceptive_ashby_urls_are_not(self):
        supported = detect_ats(
            "https://careers.example/jobs",
            '<script src="https://jobs.ashbyhq.com/acme/widget.js"></script>',
        )

        self.assertEqual(supported.status, "MATCH")
        self.assertEqual(supported.provider, "ashby")
        self.assertEqual(supported.evidence, ("SCRIPT_URL",))
        self.assertFalse(supported.application_supported)

        for url in (
            "https://jobs.ashbyhq.com.attacker.example/widget.js",
            "https://attacker.example/assets/jobs.ashbyhq.com/widget.js",
            "https://jobs.ashbyhq.com@attacker.example/widget.js",
            "https://jobs.ashbyhq.com:invalid/widget.js",
        ):
            with self.subTest(url=url):
                result = detect_ats(
                    "https://careers.example/jobs",
                    f'<script src="{url}"></script>',
                )

                self.assertEqual(result.status, "NO_MATCH")
                self.assertIsNone(result.provider)
                self.assertEqual(result.candidates, ())
                self.assertFalse(result.application_supported)


if __name__ == "__main__":
    unittest.main()
