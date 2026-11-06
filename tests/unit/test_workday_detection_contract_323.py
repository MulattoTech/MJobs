import unittest

from services.ats_detection.detector import detect_ats


class WorkdayDetectionContractTests(unittest.TestCase):
    def test_workday_shaped_job_path_on_custom_host_is_not_enough_to_match(self):
        result = detect_ats(
            "https://careers.example/job/US/Engineer_R123",
        )

        self.assertEqual(result.status, "NO_MATCH")
        self.assertIsNone(result.provider)
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.candidates, ())
        self.assertFalse(result.application_supported)


if __name__ == "__main__":
    unittest.main()
