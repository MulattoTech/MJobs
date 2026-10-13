"""Conditional-field admission rejects unsupported or changed control identity."""
import copy
import unittest

from services.applications.mock_dynamic_review import _fields
from tests.integration.test_spine import PROFILE


class MockDynamicFieldContractTests(unittest.TestCase):
    def setUp(self):
        self.row = {"question_id": "region", "question": "State or province", "supported": True,
            "sensitivity": "STANDARD", "ambiguous": False, "type": "select", "manual_review_required": False,
            "condition": {"question_id": "country", "equals": "United States"}, "answer_ids": ["state"],
            "options": ["Oklahoma", "Texas"]}
        self.control = {"question_id": "region", "id": "region", "name": "region", "tag": "select",
            "input_type": "select-one", "required": True, "repeatable": False,
            "label": "State or province", "options": [{"label": "Oklahoma", "value": "OK"}, {"label": "Texas", "value": "TX"}]}

    def test_exact_known_dropdown_keeps_value_mapping_not_guessed_answer(self):
        fields = _fields(PROFILE, {"mappings": [self.row]}, [self.control])
        self.assertEqual(fields[0]["options"][0], {"label": "Oklahoma", "value": "OK"})
        self.assertNotIn("value", fields[0])

    def test_changed_or_ambiguous_metadata_requires_review(self):
        changes = [{"question_id": "unknown"}, {"tag": "input"}, {"input_type": "select-multiple"},
            {"required": False}, {"repeatable": True}, {"label": "Account password"},
            {"name": 'x"]'}, {"options": [{"label": "Oklahoma", "value": "OK"}]},
            {"options": [{"label": "Oklahoma", "value": "same"}, {"label": "Texas", "value": "same"}]}]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(_fields(PROFILE, {"mappings": [self.row]}, [{**self.control, **change}]))
        for change in [{"sensitivity": "SENSITIVE"}, {"ambiguous": True}, {"supported": False},
                       {"answer_ids": ["a", "b"]}, {"profile_paths": ["password"]},
                       {"condition": {"question_id": "country", "equals": "Canada"}}]:
            with self.subTest(change=change):
                self.assertIsNone(_fields(PROFILE, {"mappings": [{**self.row, **change}]}, [self.control]))
        self.assertIsNone(_fields(PROFILE, {"mappings": [self.row]}, [self.control, self.control]))
        self.assertIsNone(_fields(PROFILE, {"mappings": [self.row, self.row]}, [self.control]))

    def test_unapproved_dependency_cannot_select_conditional_branch(self):
        profile = copy.deepcopy(PROFILE)
        profile["answers"][0]["user_approved"] = False
        self.assertIsNone(_fields(profile, {"mappings": [self.row]}, [self.control]))
