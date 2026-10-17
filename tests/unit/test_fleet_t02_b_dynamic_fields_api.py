"""T02 regression coverage for Workday dynamic-form approval freshness."""
import unittest

from apps.api.dynamic_application_fields import _workday_dynamic_fields


class FleetT02DynamicFormStateTests(unittest.TestCase):
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
