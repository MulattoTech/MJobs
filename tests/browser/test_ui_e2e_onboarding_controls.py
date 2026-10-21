"""Chromium-only selectors used by the connected onboarding journey."""
import unittest

from playwright.sync_api import sync_playwright

from cli import ui_e2e


class OnboardingBrowserControlTests(unittest.TestCase):
    def test_answer_bank_confirmation_locator_targets_aria_labeled_div(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.set_content("""
                  <fieldset role="group" aria-label="Question 1">
                    <div class="provenance" aria-label="Answer Bank confirmation for question 1">
                      <label><input type="checkbox" aria-label="I confirm this answer is accurate and may be reused"></label>
                    </div>
                  </fieldset>
                """)
                group = page.get_by_role("group", name="Question 1")
                confirmation = ui_e2e.answer_bank_confirmation_locator(group, 1)
                self.assertEqual(confirmation.count(), 1)
                self.assertEqual(
                    group.get_by_role("region", name="Answer Bank confirmation for question 1").count(), 0)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
