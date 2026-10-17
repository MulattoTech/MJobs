"""Synthetic Chromium check for Workday's value-free DOM metadata boundary."""
import json
import unittest

from playwright.sync_api import sync_playwright

from services.applications.adapters.workday import WorkdayApplicationAdapter
from services.applications.adapters.workday_forms import WORKDAY_DOM_METADATA_SCRIPT


class FleetT02WorkdayDomMetadataPrivacyTests(unittest.TestCase):
    def test_browser_payload_and_analysis_exclude_answer_and_selection_markers(self):
        private_markers = (
            "PRIVATE_INPUT_VALUE", "PRIVATE_TEXTAREA_ANSWER", "PRIVATE_OPTION_VALUE",
        )
        markup = """
        <form>
          <label for="country">Country</label>
          <select id="country" name="country" aria-controls="region-question">
            <option value="PRIVATE_OPTION_VALUE" selected>Ontario</option>
            <option value="quebec">Quebec</option>
          </select>
          <section id="region-question">
            <label for="region">Region</label>
            <input id="region" name="region" value="PRIVATE_INPUT_VALUE">
          </section>
          <label for="notes">Notes</label>
          <textarea id="notes" name="notes">PRIVATE_TEXTAREA_ANSWER</textarea>
        </form>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(markup)
                payload = page.locator("form").evaluate_all(WORKDAY_DOM_METADATA_SCRIPT)
                report = WorkdayApplicationAdapter().analyze_page(page)

                for marker in private_markers:
                    self.assertNotIn(marker, json.dumps(payload))
                    self.assertNotIn(marker, json.dumps(report))
                self.assertEqual(len(payload["fields"]), 3)
                self.assertEqual(report["status"], "REVIEW_REQUIRED")
                self.assertEqual(report["fields"][1]["conditional_on"], "country")
            finally:
                browser.close()

    def test_challenge_markers_outside_the_form_block_projection(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<div class="cf-turnstile"></div>'
                    '<form><label for="email">Email</label>'
                    '<input id="email" name="email" type="email"></form>'
                )
                payload = page.locator("form").evaluate_all(WORKDAY_DOM_METADATA_SCRIPT)
                self.assertEqual(payload["status"], "UNKNOWN")
                self.assertEqual(payload["reason"], "CHALLENGE_PROTECTED")
                self.assertEqual(payload["fields"], [])
            finally:
                browser.close()

    def test_hidden_question_enters_projection_when_rendered(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    '<form><label for="state">State</label><select id="state" name="state">'
                    '<option value="sensitive-a">Ontario</option>'
                    '<option value="sensitive-b">Quebec</option></select>'
                    '<div id="dependent" hidden><label for="region">Region</label>'
                    '<input id="region" name="region" value="PRIVATE"></div></form>'
                )
                adapter = WorkdayApplicationAdapter()
                before_payload = page.locator("form").evaluate_all(WORKDAY_DOM_METADATA_SCRIPT)
                before = adapter.analyze_page(page)
                self.assertEqual([field["id"] for field in before_payload["fields"]], ["state"])
                page.locator("#dependent").evaluate("node => node.hidden = false")
                after_payload = page.locator("form").evaluate_all(WORKDAY_DOM_METADATA_SCRIPT)
                result = adapter.validate_page(page, before)
                self.assertEqual([field["id"] for field in after_payload["fields"]], ["state", "region"])
                self.assertEqual(result, {"status": "UNKNOWN", "reason": "FORM_CHANGED"})
                self.assertEqual(len(before["fields"][0]["options_fingerprint"]), 64)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
