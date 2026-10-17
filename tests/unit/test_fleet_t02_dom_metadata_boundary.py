"""Privacy boundary tests for Workday's rendered DOM projection."""
import json
import hashlib
import re
import unittest



class WorkdayDomMetadataBoundaryTests(unittest.TestCase):



    def test_available_option_labels_invalidate_but_answer_selection_does_not(self):
        from services.applications.adapters.workday_forms import (
            analyze_workday_form_html, validate_workday_form_html,
        )

        template = ('<form><label for="state">State</label><select id="state" name="state">'
                    '<option value="{v1}"{s1}>{l1}</option>'
                    '<option value="{v2}"{s2}>{l2}</option></select></form>')
        first_html = template.format(v1="private-a", s1=" selected", l1="Ontario",
                                     v2="private-b", s2="", l2="Quebec")
        answer_only_html = template.format(v1="other-private-a", s1="", l1="Ontario",
                                           v2="other-private-b", s2=" selected", l2="Quebec")
        changed_html = template.format(v1="x", s1="", l1="Texas", v2="y", s2="", l2="Utah")
        baseline = analyze_workday_form_html(first_html)
        self.assertNotEqual(baseline["status"], "UNKNOWN")
        same_choices = analyze_workday_form_html(answer_only_html)
        self.assertEqual(validate_workday_form_html(answer_only_html, baseline), same_choices)
        self.assertEqual(validate_workday_form_html(changed_html, baseline), {
            "provider": "workday", "status": "UNKNOWN", "reason": "FORM_CHANGED", "fields": [],
        })
        fingerprint = baseline["fields"][0]["options_fingerprint"]
        self.assertRegex(fingerprint, re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(fingerprint, hashlib.sha256(b'["Ontario","Quebec"]').hexdigest())
        self.assertNotIn("private", json.dumps(baseline))


    def test_native_option_label_attribute_participates_in_static_html_fingerprint(self):
        from services.applications.adapters.workday_forms import (
            analyze_workday_form_html, validate_workday_form_html,
        )

        before_html = (
            '<form><label for="country">Country</label><select id="country">'
            '<option label="Preferred country">Choose here</option>'
            '<option>Canada</option></select></form>'
        )
        after_html = (
            '<form><label for="country">Country</label><select id="country">'
            '<option label="Country of residence">Choose here</option>'
            '<option>Canada</option></select></form>'
        )
        expected = analyze_workday_form_html(before_html)
        self.assertNotEqual(expected["status"], "UNKNOWN")
        self.assertEqual(
            expected["fields"][0]["options_fingerprint"],
            hashlib.sha256(b'["Preferred country","Canada"]').hexdigest(),
        )
        self.assertEqual(
            validate_workday_form_html(after_html, expected),
            {"provider": "workday", "status": "UNKNOWN", "reason": "FORM_CHANGED", "fields": []},
        )


if __name__ == "__main__":
    unittest.main()
