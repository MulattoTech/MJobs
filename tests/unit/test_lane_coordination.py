import unittest

from tools.coordination.lane_coordination import paths_overlap


class LaneCoordinationTests(unittest.TestCase):
    def test_exact_and_recursive_claim_overlap(self):
        self.assertTrue(paths_overlap("services/ai/**", "services/ai/tasks.py"))
        self.assertTrue(paths_overlap("apps/web/src/main.tsx", "apps/web/src/main.tsx"))
        self.assertFalse(paths_overlap("services/ai/**", "services/discovery/sources/remotive.py"))

    def test_recursive_claim_does_not_match_similar_prefix(self):
        self.assertFalse(paths_overlap("services/ai/**", "services/audit/report.py"))


if __name__ == "__main__":
    unittest.main()
