"""Pure dynamic form-state evaluation for synthetic mock application forms."""
import unittest

from services.applications.dynamic_form_state import reconcile_dynamic_form_state


FORM = {
    "schema_version": 1,
    "questions": [
        {"question_id": "country", "kind": "select", "required": True,
         "options": ["United States", "Canada"]},
        {"question_id": "region", "kind": "select", "required": True,
         "condition": {"question_id": "country", "equals": "United States"},
         "options_by": {"question_id": "country", "choices": {
             "United States": ["Oklahoma", "Texas"],
             "Canada": ["Ontario", "Quebec"],
         }}},
        {"question_id": "has_experience", "kind": "select", "required": True,
         "options": ["yes", "no"]},
        {"question_id": "years", "kind": "text", "required": True,
         "condition": {"question_id": "has_experience", "equals": "yes"}},
        {"question_id": "note", "kind": "text", "required": False,
         "section_id": "optional_details"},
    ],
    "optional_sections": [{
        "section_id": "optional_details", "question_ids": ["note"],
        "condition": {"question_id": "has_experience", "equals": "yes"},
    }],
    "repeatable_groups": [{
        "group_id": "experience", "section_id": "optional_details",
        "minimum_rows": 0, "maximum_rows": 3,
        "fields": [
            {"question_id": "employer", "kind": "text", "required": True},
            {"question_id": "role", "kind": "text", "required": True},
        ],
    }],
}


class DynamicFormStateTests(unittest.TestCase):
    def test_conditional_visibility_and_newly_required_question_follow_answers(self):
        initial = reconcile_dynamic_form_state(FORM, {})
        self.assertEqual(initial["status"], "PASS")
        self.assertNotIn("region", initial["visible_field_ids"])
        self.assertNotIn("years", initial["visible_field_ids"])

        answered = reconcile_dynamic_form_state(
            FORM, {"country": "United States", "has_experience": "yes"},
            previous_state=initial,
        )
        self.assertIn("region", answered["visible_field_ids"])
        self.assertIn("years", answered["visible_field_ids"])
        self.assertEqual(set(answered["newly_visible_field_ids"]), {"region", "years"})
        self.assertEqual(set(answered["newly_required_field_ids"]), {"region", "years"})
        self.assertIn("region", answered["unresolved_required_field_ids"])
        self.assertIn("years", answered["unresolved_required_field_ids"])

        recovered = reconcile_dynamic_form_state(
            FORM, {"country": "Canada", "has_experience": "no", "years": "kept locally"},
            previous_state=answered,
        )
        self.assertNotIn("region", recovered["visible_field_ids"])
        self.assertNotIn("years", recovered["visible_field_ids"])
        self.assertEqual(set(recovered["newly_hidden_field_ids"]), {"region", "years"})
        self.assertNotIn("kept locally", str(recovered))

    def test_dependent_choices_refresh_and_reject_values_outside_current_parent_choices(self):
        us = reconcile_dynamic_form_state(
            FORM, {"country": "United States", "region": "Oklahoma"},
        )
        self.assertEqual(us["dependent_choices"]["region"], ["Oklahoma", "Texas"])
        region = next(field for field in us["fields"] if field["field_id"] == "region")
        self.assertTrue(region["answer_valid"])

        invalid = reconcile_dynamic_form_state(
            FORM, {"country": "United States", "region": "Ontario", "has_experience": "no"},
        )
        self.assertEqual(invalid["dependent_choices"]["region"], ["Oklahoma", "Texas"])
        self.assertIn("region", invalid["unresolved_required_field_ids"])
        self.assertEqual(invalid["fields_by_id"]["region"]["reason"], "ANSWER_NOT_IN_CURRENT_CHOICES")

    def test_stale_answer_from_hidden_parent_does_not_activate_descendants(self):
        schema = {**FORM, "questions": [
            {**FORM["questions"][0],
             "condition": {"question_id": "has_experience", "equals": "yes"}},
            *FORM["questions"][1:],
        ]}
        result = reconcile_dynamic_form_state(
            schema,
            {"has_experience": "no", "country": "United States", "region": "Oklahoma"},
        )
        self.assertNotIn("country", result["visible_field_ids"])
        self.assertNotIn("region", result["visible_field_ids"])
        self.assertEqual(result["dependent_choices"]["region"], [])
        self.assertNotIn("country", result["required_field_ids"])
        self.assertNotIn("region", result["required_field_ids"])

    def test_optional_section_and_repeatable_rows_expand_without_static_row_assumptions(self):
        baseline_answers = {"country": "United States", "region": "Oklahoma", "has_experience": "yes", "years": "4"}
        hidden = reconcile_dynamic_form_state(FORM, baseline_answers)
        self.assertNotIn("note", hidden["visible_field_ids"])
        self.assertEqual(hidden["optional_sections"]["optional_details"]["expanded"], False)

        expanded = reconcile_dynamic_form_state(
            FORM, {**baseline_answers, "note": "synthetic local note"},
            expanded_sections=["optional_details"],
            repeatable_rows={"experience": ["work-a", "work-b"]},
            previous_state=hidden,
        )
        self.assertIn("note", expanded["visible_field_ids"])
        self.assertTrue(expanded["optional_sections"]["optional_details"]["expanded"])
        group = expanded["repeatable_groups"]["experience"]
        self.assertEqual(group["row_ids"], ["work-a", "work-b"])
        self.assertEqual(group["field_ids_by_row"]["work-a"], [
            "experience[work-a].employer", "experience[work-a].role",
        ])
        self.assertEqual(set(expanded["newly_visible_field_ids"]), {
            "note", "experience[work-a].employer", "experience[work-a].role",
            "experience[work-b].employer", "experience[work-b].role",
        })
        self.assertEqual(set(expanded["unresolved_required_field_ids"]), {
            "experience[work-a].employer", "experience[work-a].role",
            "experience[work-b].employer", "experience[work-b].role",
        })
        self.assertNotIn("synthetic local note", str(expanded))

    def test_repeatable_minimum_rows_and_row_removal_are_recomputed(self):
        schema = {**FORM, "repeatable_groups": [{
            **FORM["repeatable_groups"][0], "minimum_rows": 1,
        }]}
        empty = reconcile_dynamic_form_state(
            schema, {"has_experience": "yes"}, expanded_sections=["optional_details"],
        )
        self.assertIn("experience.__minimum_rows__", empty["unresolved_required_field_ids"])

        one_row = reconcile_dynamic_form_state(
            schema, {"has_experience": "yes"}, expanded_sections=["optional_details"],
            repeatable_rows={"experience": ["work-a"]}, previous_state=empty,
        )
        self.assertNotIn("experience.__minimum_rows__", one_row["unresolved_required_field_ids"])
        self.assertEqual(len(one_row["newly_visible_field_ids"]), 2)

        removed = reconcile_dynamic_form_state(
            schema, {"has_experience": "yes"}, expanded_sections=["optional_details"],
            repeatable_rows={"experience": []}, previous_state=one_row,
        )
        self.assertIn("experience.__minimum_rows__", removed["unresolved_required_field_ids"])
        self.assertEqual(set(removed["newly_hidden_field_ids"]), {
            "experience[work-a].employer", "experience[work-a].role",
        })

    def test_invalid_schema_or_state_fails_closed_without_echoing_user_values(self):
        invalid_schemas = (
            {**FORM, "schema_version": 2},
            {**FORM, "questions": [*FORM["questions"], FORM["questions"][0]]},
            {**FORM, "questions": [{"question_id": "secret", "required": True,
                                      "condition": {"question_id": "missing", "equals": "private"}}]},
        )
        for schema in invalid_schemas:
            with self.subTest(schema=schema):
                result = reconcile_dynamic_form_state(schema, {"secret": "PRIVATE_ANSWER"})
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["fields"], [])
                self.assertNotIn("PRIVATE_ANSWER", str(result))

        invalid_state = reconcile_dynamic_form_state(
            FORM, {"unknown-question": "PRIVATE_ANSWER"},
        )
        self.assertEqual(invalid_state["status"], "UNKNOWN")
        self.assertEqual(invalid_state["reason"], "UNKNOWN_ANSWER_REFERENCE")
        self.assertNotIn("PRIVATE_ANSWER", str(invalid_state))

    def test_reconciliation_is_deterministic_for_recovered_input(self):
        answers = {"country": "United States", "region": "Oklahoma", "has_experience": "yes"}
        first = reconcile_dynamic_form_state(
            FORM, answers, expanded_sections=["optional_details"],
            repeatable_rows={"experience": ["work-a"]},
        )
        recovered = reconcile_dynamic_form_state(
            FORM, dict(answers), expanded_sections=list(reversed(["optional_details"])),
            repeatable_rows={"experience": ["work-a"]},
        )
        self.assertEqual(recovered, first)


if __name__ == "__main__":
    unittest.main()
