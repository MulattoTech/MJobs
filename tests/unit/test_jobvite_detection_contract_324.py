import unittest

from services.ats_detection.detector import detect_ats


class JobviteDetectionContract324Tests(unittest.TestCase):
    def test_jobvite_redirect_hostname_is_evidence_but_deceptive_hosts_are_not(self):
        result = detect_ats(
            "https://careers.acme.example/jobs",
            redirect_chain=["https://jobs.jobvite.com/acme"],
        )

        self.assertEqual(result.status, "MATCH")
        self.assertEqual(result.provider, "jobvite")
        self.assertIn("REDIRECT_HOSTNAME", result.evidence)
        self.assertFalse(result.application_supported)

        deceptive_redirects = (
            "https://jobs.jobvite.com.attacker.example/acme",
            "https://jobvite.com@attacker.example/acme",
        )
        for redirect in deceptive_redirects:
            with self.subTest(redirect=redirect):
                result = detect_ats(
                    "https://careers.acme.example/jobs",
                    redirect_chain=[redirect],
                )
                self.assertEqual(result.status, "NO_MATCH")
                self.assertIsNone(result.provider)
                self.assertEqual(result.candidates, ())
                self.assertFalse(result.application_supported)


if __name__ == "__main__":
    unittest.main()
