"""PREVIOUS navigation may return to a page on the active route stack."""
import unittest

from services.applications.adapters.workday_navigation import (
    classify_step,
    decide_transition,
)


def observation(path, heading, *, stale=False):
    return {
        "url": f"https://wd5.myworkdayjobs.com/acme/job/REQ-1/{path}",
        "headings": [heading],
        "field_labels": [heading],
        "actions": ["Save and Continue"],
        "stale": stale,
    }


class FleetT03AWorkdayPreviousTests(unittest.TestCase):
    def test_previous_accepts_known_adjacent_destination_already_on_route_stack(self):
        experience = classify_step(observation("experience", "My Experience"))
        questions = classify_step(observation("questions", "Application Questions"))
        information = classify_step(observation("information", "My Information"))
        visited = [information["fingerprint"], experience["fingerprint"],
                   questions["fingerprint"]]

        result = decide_transition(
            questions, experience, "PREVIOUS", visited_fingerprints=visited,
        )

        self.assertEqual(result["decision"], "CONFIRMED")
        self.assertEqual(result["reason"], "CHANGED_PAGE_MATCHES_EXPECTED_STEP")

    def test_next_still_rejects_a_visited_destination_as_a_loop(self):
        information = classify_step(observation("information", "My Information"))
        experience = classify_step(observation("experience", "My Experience"))

        result = decide_transition(
            information, experience, "NEXT",
            visited_fingerprints=[experience["fingerprint"]],
        )

        self.assertEqual(result["decision"], "LOOP_DETECTED")

    def test_previous_still_rejects_no_change_unknown_and_stale_observations(self):
        experience = classify_step(observation("experience", "My Experience"))
        questions = classify_step(observation("questions", "Application Questions"))
        stale = classify_step(observation("experience", "My Experience", stale=True))
        unknown = classify_step(observation("unknown", "Additional Details"))

        unchanged = decide_transition(experience, experience, "PREVIOUS")
        stale_result = decide_transition(questions, stale, "PREVIOUS")
        unknown_result = decide_transition(questions, unknown, "PREVIOUS")

        self.assertEqual(unchanged["decision"], "WAIT_FOR_OBSERVED_CHANGE")
        self.assertEqual(stale_result["decision"], "REFRESH_AND_RECLASSIFY")
        self.assertEqual(unknown_result["decision"], "MANUAL_REVIEW")


if __name__ == "__main__":
    unittest.main()
