"""Lever ATS detection contract gaps tracked by issue #319."""

import unittest

from services.ats_detection.detector import detect_ats


class LeverDetectionContract319Tests(unittest.TestCase):
    def test_public_lever_listing_and_apply_routes_are_structural_board_evidence(self):
        job_id = "123e4567-e89b-12d3-a456-426614174000"
        for route in (job_id, f"{job_id}/apply"):
            with self.subTest(route=route):
                result = detect_ats(
                    "https://careers.example/jobs",
                    f'<a href="https://jobs.lever.co/acme/{route}">View job</a>',
                )
                self.assertEqual(result.status, "MATCH")
                self.assertEqual(result.provider, "lever")
                self.assertEqual(result.evidence, ("BOARD_LINK",))
                self.assertFalse(result.application_supported)

    def test_deceptive_and_malformed_lever_board_links_are_not_evidence(self):
        for url in (
            "https://jobs.lever.co.attacker.example/acme",
            "http://jobs.lever.co/acme",
            "https://user@jobs.lever.co/acme",
            "https://jobs.lever.co:8443/acme",
            "https://jobs.lever.co/acme/not-a-job-uuid",
            "https://jobs.lever.co/acme#jobs",
        ):
            with self.subTest(url=url):
                result = detect_ats(
                    "https://careers.example/jobs",
                    f'<a href="{url}">Jobs</a>',
                )
                self.assertEqual(result.status, "NO_MATCH")
                self.assertIsNone(result.provider)
                self.assertEqual(result.evidence, ())
                self.assertFalse(result.application_supported)


if __name__ == "__main__":
    unittest.main()
