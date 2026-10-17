"""T02 regression coverage for Workday dynamic-form approval freshness."""
import unittest

from apps.api.dynamic_application_fields import (
    _field_type,
    _workday_dynamic_fields,
    _workday_field_type,
)


class FleetT02DynamicFormStateTests(unittest.TestCase):
    def test_workday_type_aliases_do_not_change_generic_mapping(self):
        for alias in ("tel", "url", "password"):
            self.assertIsNone(_field_type(alias))
            self.assertEqual(_workday_field_type(alias), "text")

    def test_workday_envelope_projection_never_offers_fillable_answer_fields(self):
        fields = _workday_dynamic_fields({"fields": [{
            "id": "region", "name": "region", "type": "text", "required": True,
            "conditional": True, "conditional_on": "country", "repeatable": False,
            "repeatable_group": None,
        }]})
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["depends_on"], "country")
        self.assertFalse(fields[0]["supported"])
        self.assertTrue(fields[0]["manual_review_required"])
        self.assertEqual(fields[0]["options"], [])


if __name__ == "__main__":
    unittest.main()
