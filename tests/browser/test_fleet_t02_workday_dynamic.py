"""Synthetic Chromium negative controls for the Workday DOM review boundary."""
import unittest

from playwright.sync_api import sync_playwright

from services.applications.adapters.workday import WorkdayApplicationAdapter
from services.applications.workday_dynamic_review import project_workday_dynamic_review


FIXTURE = """<!doctype html><form>
<label for="country">Country</label><select id="country" name="country"
 data-workday-conditional="false" data-workday-repeatable="false">
 <option>Choose</option><option>Canada</option></select>
<div id="dependent"></div><div id="rows"></div>
<div id="optional" style="display:none"><label for="note">Optional note</label>
<input id="note" name="note" data-workday-conditional="false"
 data-workday-repeatable="false"></div>
<button type="button" id="add">Add experience</button>
</form><script>
document.querySelector('#country').addEventListener('change', () => {
 document.querySelector('#dependent').innerHTML = `
 <label for="region">Province</label><select id="region" name="region" required
 data-workday-conditional="true" data-workday-conditional-on="country"
 data-workday-repeatable="false"><option>Ontario</option><option>Quebec</option></select>`;
});
let row = 0;
document.querySelector('#add').addEventListener('click', () => {
 row += 1;
 document.querySelector('#rows').insertAdjacentHTML('beforeend', `
 <label for="employer-${row}">Employer</label><input id="employer-${row}"
 name="employer-${row}" data-workday-conditional="false"
 data-workday-repeatable="true" data-workday-repeatable-group="experience">`);
});
</script>"""


class FleetT02WorkdayDynamicDOMTests(unittest.TestCase):
    def test_user_changes_invalidate_loaded_dom_observation_without_reading_answers(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                fixture_url = "https://fixture.invalid/t02-workday"
                context.route("**/*", lambda route: route.fulfill(
                    body=FIXTURE, content_type="text/html",
                ) if route.request.url == fixture_url else route.abort())
                page = context.new_page()
                page.goto(fixture_url)
                adapter = WorkdayApplicationAdapter()
                initial = adapter.analyze_page(page)
                first = project_workday_dynamic_review(initial)
                self.assertIsNotNone(first)
                page.locator("#country").select_option(label="Canada")
                conditional = adapter.analyze_page(page)
                second = project_workday_dynamic_review(conditional)
                self.assertIsNotNone(second)
                self.assertNotEqual(first["fingerprint"], second["fingerprint"])
                self.assertEqual(adapter.validate_page(page, initial).get("reason"), "FORM_CHANGED")
                fields = {field["id"]: field for field in second["fields"]}
                self.assertEqual(fields["region"]["conditional_on"], "country")
                page.locator("#add").click()
                repeated = project_workday_dynamic_review(adapter.analyze_page(page))
                self.assertNotEqual(second["fingerprint"], repeated["fingerprint"])
                page.locator("#employer-1").fill("PRIVATE_BROWSER_APPLICANT")
                answered = project_workday_dynamic_review(adapter.analyze_page(page))
                self.assertEqual(answered, repeated)
                self.assertNotIn("PRIVATE_BROWSER_APPLICANT", str(answered))
                page.locator("#region").evaluate(
                    "field => field.replaceChildren(new Option('Texas'), new Option('Utah'))")
                choices_changed = project_workday_dynamic_review(adapter.analyze_page(page))
                self.assertNotEqual(choices_changed["fingerprint"], answered["fingerprint"])
                self.assertNotIn("note", {field["id"] for field in choices_changed["fields"]})
                page.locator("#optional").evaluate("node => node.style.display = 'block'")
                revealed = project_workday_dynamic_review(adapter.analyze_page(page))
                self.assertIn("note", {field["id"] for field in revealed["fields"]})
                self.assertNotEqual(revealed["fingerprint"], choices_changed["fingerprint"])
                page.locator("#optional").evaluate("node => node.style.opacity = '0'")
                transparent = project_workday_dynamic_review(adapter.analyze_page(page))
                self.assertNotIn("note", {field["id"] for field in transparent["fields"]})
                page.locator("form").evaluate(
                    "form => form.insertAdjacentHTML('beforeend', '<div id=editable contenteditable></div>')")
                self.assertIsNone(project_workday_dynamic_review(adapter.analyze_page(page)))
                page.locator("#editable").evaluate("node => node.remove()")
                self.assertFalse(adapter.capabilities()["submission"])
                self.assertFalse(adapter.capabilities()["live_form_interaction"])
                page.locator("body").evaluate(
                    "body => body.insertAdjacentHTML('beforeend', '<div id=recaptcha></div>')")
                challenged = adapter.analyze_page(page)
                self.assertIsNone(project_workday_dynamic_review(challenged))
                self.assertEqual(challenged["status"], "UNKNOWN")
            finally:
                browser.close()
