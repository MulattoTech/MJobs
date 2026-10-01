import unittest

from services.discovery.negative_filters import parse_negative_conditions
from services.discovery.search_semantics import evaluate_search


class NegativeFilterTests(unittest.TestCase):
    def test_detects_explicit_negative_conditions_and_travel(self):
        parsed = parse_negative_conditions(
            "This role requires on-call rotation and night shift coverage. "
            "1099 contract with commission-based compensation and door-to-door visits. "
            "Relocation is required. Travel up to 40%."
        )
        self.assertEqual(
            set(parsed["flags"]),
            {"ON_CALL", "NIGHT_SHIFT", "CONTRACT_1099", "COMMISSION", "DOOR_TO_DOOR", "MANDATORY_RELOCATION"},
        )
        self.assertEqual(parsed["travel_percent"], 40)
        self.assertTrue(all(row.get("raw") for row in parsed["evidence"]))

    def test_clearance_is_structured_negative_condition_when_present(self):
        parsed = parse_negative_conditions(
            "Commercial role.",
            {"level": "SECRET", "raw": "Secret", "clause": "Secret clearance required"},
        )
        self.assertIn("CLEARANCE_REQUIRED", parsed["flags"])
        self.assertEqual(parsed["travel_percent"], None)

    def test_default_search_does_not_assume_negative_preferences(self):
        job = {
            "title": "Field Engineer", "company": "Example", "description": "", "skills": [],
            "location": "Remote", "remote_type": "REMOTE_US",
            "attributes": {
                "negative_conditions": {
                    "flags": ["ON_CALL", "COMMISSION"],
                    "travel_percent": 50,
                    "evidence": [],
                }
            },
        }
        result = evaluate_search({}, job)
        self.assertTrue(result["matched"], result["reasons"])

    def test_explicit_negative_filters_exclude_only_selected_conditions(self):
        job = {
            "title": "Field Engineer", "company": "Example", "description": "", "skills": [],
            "location": "Remote", "remote_type": "REMOTE_US",
            "attributes": {
                "negative_conditions": {
                    "flags": ["ON_CALL", "COMMISSION"],
                    "travel_percent": 50,
                    "evidence": [],
                }
            },
        }
        result = evaluate_search({
            "excluded_conditions": ["ON_CALL"],
            "max_travel_percent": 25,
        }, job)
        self.assertFalse(result["matched"])
        self.assertIn("NEGATIVE_CONDITION:ON_CALL", result["reasons"])
        self.assertIn("TRAVEL_EXCEEDS_MAXIMUM", result["reasons"])
        self.assertNotIn("NEGATIVE_CONDITION:COMMISSION", result["reasons"])
    def test_unknown_travel_does_not_get_invented_or_excluded(self):
        job = {
            "title": "Engineer", "company": "Example", "description": "", "skills": [],
            "location": "Remote", "remote_type": "REMOTE_US",
            "attributes": {"negative_conditions": {"flags": [], "travel_percent": None, "evidence": []}},
        }
        result = evaluate_search({"max_travel_percent": 25}, job)
        self.assertTrue(result["matched"], result["reasons"])


if __name__ == "__main__":
    unittest.main()
