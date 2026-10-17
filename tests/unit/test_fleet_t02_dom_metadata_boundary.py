"""Privacy boundary tests for Workday's rendered DOM projection."""
import json
import hashlib
import re
import unittest

from services.applications.adapters.workday import WorkdayApplicationAdapter


class WorkdayDomMetadataBoundaryTests(unittest.TestCase):
    def test_page_evaluation_transfers_only_bounded_metadata(self):
        markers = ("PRIVATE_INPUT_VALUE", "PRIVATE_TEXTAREA_ANSWER", "PRIVATE_OPTION")
        payload = {
            "fields": [{
                "id": "country", "name": "country", "label": "Country",
                "type": "select", "required": False,
                "conditional": None, "conditional_on": None,
                "repeatable": None, "repeatable_group": None,
            }],
        }

        class Page:
            def locator(self, selector):
                if selector != "form":
                    raise AssertionError("Only the form locator is expected")
                return self

            def evaluate_all(self, script, *, timeout):
                self.script = script
                self.timeout = timeout
                return payload

        page = Page()
        report = WorkdayApplicationAdapter().analyze_page(page)

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertNotIn("outerHTML", page.script)
        self.assertNotIn(".value", page.script)
        self.assertNotIn("selected", page.script.casefold())
        self.assertNotIn("checked", page.script.casefold())
        self.assertIn("MAX_WORKDAY_MARKUP_EVENTS", page.script)
        self.assertIn("MAX_WORKDAY_DOM_FIELDS", page.script)
        self.assertIn("getClientRects", page.script)
        self.assertIn("options_fingerprint", page.script)
        self.assertTrue(all(marker not in json.dumps(report) for marker in markers))
        self.assertEqual(page.timeout, 10000)

    def test_rendered_page_validation_rejects_a_changed_safe_question_projection(self):
        first = {"fields": [{
            "id": "email", "name": "email", "label": "Email", "type": "email",
            "required": False, "conditional": False, "conditional_on": None,
            "repeatable": False, "repeatable_group": None,
        }]}
        second = {"fields": [{
            "id": "phone", "name": "phone", "label": "Phone", "type": "tel",
            "required": False, "conditional": False, "conditional_on": None,
            "repeatable": False, "repeatable_group": None,
        }]}

        class Page:
            def __init__(self, data):
                self.data = data

            def locator(self, selector):
                return self

            def evaluate_all(self, script, *, timeout):
                return self.data

        adapter = WorkdayApplicationAdapter()
        expected = adapter.analyze_page(Page(first))

        result = adapter.validate_page(Page(second), expected)

        self.assertEqual(result, {"status": "UNKNOWN", "reason": "FORM_CHANGED"})

    def test_page_projection_preserves_cancellation_checks_and_remaining_deadline(self):
        payload = {"fields": [{
            "id": "email", "name": "email", "label": "Email", "type": "email",
            "required": False, "conditional": False, "conditional_on": None,
            "repeatable": False, "repeatable_group": None,
        }]}

        class Check:
            calls = 0

            def __call__(self):
                self.calls += 1

            def remaining_seconds(self):
                return 0.25

        class Page:
            timeout = None

            def locator(self, selector):
                return self

            def evaluate_all(self, script, *, timeout):
                self.timeout = timeout
                return payload

        page = Page()
        check = Check()
        WorkdayApplicationAdapter().analyze_page(page, check=check)

        self.assertEqual(page.timeout, 250)
        self.assertGreaterEqual(check.calls, 2)

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

    def test_dom_option_labels_are_hashed_and_removed_from_projection(self):
        from services.applications.adapters.workday_forms import analyze_workday_dom_metadata

        field = {
            "id": "state", "name": "state", "label": "State", "type": "select",
            "required": False, "conditional": False, "conditional_on": None,
            "repeatable": False, "repeatable_group": None,
            "option_labels": [" Ontario ", "Quebec"],
        }
        result = analyze_workday_dom_metadata({"fields": [field]})
        self.assertEqual(result["fields"][0]["options_fingerprint"],
                         hashlib.sha256(b'["Ontario","Quebec"]').hexdigest())
        self.assertNotIn("option_labels", json.dumps(result))
        self.assertNotIn("options_fingerprint", json.dumps({"fields": [field]}))


if __name__ == "__main__":
    unittest.main()
