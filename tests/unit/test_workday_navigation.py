"""Synthetic contracts for review-first Workday page navigation decisions."""
import unittest

from services.applications.adapters.workday_navigation import (
    CAPABILITIES,
    classify_step,
    decide_transition,
)


def observation(path, heading, *, labels=(), actions=("Save and Continue",), stale=False):
    return {
        "url": f"https://wd5.myworkdayjobs.com/acme/job/{path}?session=discarded",
        "headings": [heading],
        "field_labels": list(labels),
        "actions": list(actions),
        "stale": stale,
    }


class WorkdayNavigationTests(unittest.TestCase):
    def test_capability_declaration_keeps_live_ats_actions_disabled(self):
        self.assertTrue(CAPABILITIES["step_classification"])
        self.assertTrue(CAPABILITIES["observed_transition_decisions"])
        self.assertTrue(CAPABILITIES["refresh_and_resume_safety_decisions"])
        for capability in ("browser_interaction", "form_entry", "document_upload", "submission"):
            self.assertFalse(CAPABILITIES[capability])

    def test_classifies_known_workday_application_steps_from_visible_landmarks(self):
        cases = (
            ("apply", "My Information", ["Legal Name", "Email"], "my_information"),
            ("apply", "My Experience", ["Resume"], "my_experience"),
            ("apply", "Application Questions", ["Are you authorized to work?"], "application_questions"),
            ("apply", "Voluntary Disclosures", ["Gender"], "voluntary_disclosures"),
            ("apply", "Review", [], "review"),
        )
        for path, heading, labels, expected in cases:
            with self.subTest(step=expected):
                result = classify_step(observation(path, heading, labels=labels))
                self.assertEqual(result["status"], "KNOWN")
                self.assertEqual(result["step"], expected)
                self.assertRegex(result["fingerprint"], r"^[0-9a-f]{64}$")

    def test_confirmation_requires_changed_fingerprint_and_coherent_destination(self):
        before = classify_step(observation("apply", "My Information", labels=["Email"]))
        after = classify_step(observation("apply", "My Experience", labels=["Resume"]))
        self.assertEqual(decide_transition(before, after, "NEXT")["decision"], "CONFIRMED")
        self.assertEqual(decide_transition(before, before, "NEXT")["decision"], "WAIT_FOR_OBSERVED_CHANGE")
        self.assertEqual(decide_transition(
            before, before, "NEXT", visited_fingerprints=[before["fingerprint"]]
        )["decision"], "WAIT_FOR_OBSERVED_CHANGE")
        wrong_direction = decide_transition(after, before, "NEXT")
        self.assertEqual(wrong_direction["decision"], "MANUAL_REVIEW")
        self.assertEqual(wrong_direction["reason"], "DESTINATION_NOT_COHERENT")

    def test_unknown_stale_and_changed_pages_fail_closed(self):
        known = classify_step(observation("apply", "My Experience", labels=["Resume"]))
        unknown = classify_step(observation("apply", "Additional Details", labels=["Other"] , actions=("Continue",)))
        stale = classify_step(observation("apply", "Application Questions", stale=True))
        self.assertEqual(unknown["status"], "UNKNOWN")
        self.assertEqual(decide_transition(known, unknown, "NEXT")["decision"], "MANUAL_REVIEW")
        self.assertEqual(decide_transition(known, stale, "NEXT")["decision"], "REFRESH_AND_RECLASSIFY")
        self.assertEqual(decide_transition(known, known, "REFRESH")["decision"], "REFRESH_AND_RECLASSIFY")
        non_workday = classify_step({**observation("apply", "Review"),
                                    "url": "https://example.test/apply"})
        self.assertEqual(non_workday["status"], "UNKNOWN")

    def test_fingerprint_ignores_query_string_and_invalid_loop_state_fails_closed(self):
        first = classify_step(observation("apply", "My Information", labels=["Email"]))
        changed_query = classify_step({**observation("apply", "My Information", labels=["Email"]),
                                       "url": "https://wd5.myworkdayjobs.com/acme/job/apply?session=other"})
        self.assertEqual(first["fingerprint"], changed_query["fingerprint"])
        next_page = classify_step(observation("apply", "My Experience", labels=["Resume"]))
        self.assertEqual(decide_transition(first, next_page, "NEXT", visited_fingerprints=None)["decision"],
                         "MANUAL_REVIEW")

    def test_repeated_fingerprints_detect_a_loop(self):
        before = classify_step(observation("apply", "My Experience", labels=["Resume"]))
        after = classify_step(observation("apply", "Application Questions", labels=["Question"]))
        decision = decide_transition(before, after, "NEXT", visited_fingerprints=[after["fingerprint"]])
        self.assertEqual(decision["decision"], "LOOP_DETECTED")

    def test_interrupted_resume_requires_matching_confirmed_state(self):
        confirmed = classify_step(observation("apply", "My Experience", labels=["Resume"]))
        matching = classify_step(observation("apply", "My Experience", labels=["Resume"]))
        changed = classify_step(observation("apply", "Application Questions", labels=["Question"]))
        self.assertEqual(decide_transition(confirmed, matching, "RESUME", interrupted=True,
                                           confirmed_state=confirmed)["decision"], "RESUME_FROM_CONFIRMED_STEP")
        self.assertEqual(decide_transition(confirmed, changed, "RESUME", interrupted=True,
                                           confirmed_state=confirmed)["decision"], "MANUAL_REVIEW")

    def test_final_submit_is_never_confirmed_or_executed(self):
        review = classify_step(observation("apply", "Review", actions=("Submit",)))
        submitted = classify_step(observation("application-submitted", "Thank you", actions=()))
        for observed in (review, submitted):
            decision = decide_transition(review, observed, "FINAL_SUBMIT")
            self.assertEqual(decision["decision"], "AMBIGUOUS_FINAL_ACTION")
            self.assertFalse(decision["execute"])


if __name__ == "__main__":
    unittest.main()
