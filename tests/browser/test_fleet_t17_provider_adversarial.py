"""Team 17 adversarial browser coverage for public provider review flows."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from services.applications.form_analysis import analyze_form_page
from tests.browser import test_lever_form_review as lever_flow


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "fleet_t17"
FIXTURE_COMMIT = "a874d48456f49f757de21bc4c017de7e9cf74329"
STATIC_REVIEW_FORM = b"""<!doctype html><form>
<label for="first_name">First name</label>
<input id="first_name" name="first_name" value="PRIVATE-DECOY-APPLICANT">
<label for="gender">Gender</label>
<select id="gender" name="gender"><option value="private">PRIVATE-DECOY-GENDER</option></select>
</form>"""


def _snapshot(page):
    return page.evaluate("""() => {
      const form = document.querySelector('form');
      return {
        html: form.innerHTML,
        controls: Array.from(form.querySelectorAll('input,select,textarea')).map(node => ({
          id: node.id, name: node.name, value: node.value, checked: Boolean(node.checked),
          selectedIndex: node.selectedIndex, hidden: Boolean(node.hidden),
          disabled: Boolean(node.disabled)
        })),
        events: {...window.__t17Events}
      };
    }""")


def _watch_form_events(page):
    page.evaluate("""() => {
      const form = document.querySelector('form');
      window.__t17Events = {input: 0, change: 0, click: 0, submit: 0};
      for (const name of Object.keys(window.__t17Events)) {
        form.addEventListener(name, event => {
          window.__t17Events[name]++;
          if (name === 'submit') event.preventDefault();
        }, true);
      }
    }""")


class FleetT17ProviderAdversarialBrowserTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "apps" / "web" / "dist" / "index.html").is_file(),
                         "Build apps/web before connected browser acceptance")
    def test_static_provider_review_stays_read_only_and_reads_back(self):
        # Keep static worker persistence proof separate: raw HTML cannot
        # observe the rendered overlay tested by the Chromium case below.
        harness = lever_flow.LeverFormReviewBrowserTests(
            "test_persisted_lever_route_inspects_safely_and_reads_back_after_reload"
        )
        private_markers = lever_flow.PRIVATE_MARKERS
        with patch.object(lever_flow, "FORM_FIXTURE", STATIC_REVIEW_FORM), \
                patch.object(lever_flow, "PRIVATE_MARKERS", private_markers):
            harness._assert_persisted_region_review(lever_flow.LEVER_ROUTE)

    @unittest.skipUnless(all((FIXTURES / name).is_file() for name in (
                             "visible-with-hidden-decoys.html",
                             "overlay-blocks-form.html",
                             "malformed-conditional-repeat.html")),
                         f"Integrate T17 fixture commit {FIXTURE_COMMIT} first")
    def test_chromium_dom_projection_fails_closed_without_mutation_or_value_leaks(self):
        fixture_names = (
            "visible-with-hidden-decoys.html",
            "overlay-blocks-form.html",
            "malformed-conditional-repeat.html",
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1365, "height": 900})
                for fixture_name in fixture_names:
                    with self.subTest(fixture=fixture_name):
                        markup = (FIXTURES / fixture_name).read_text(encoding="utf-8")
                        page.set_content(markup, wait_until="load")

                        if fixture_name == "visible-with-hidden-decoys.html":
                            visibility = page.evaluate("""() => {
                              const visible = id => {
                                const node = document.getElementById(id);
                                const style = getComputedStyle(node);
                                return style.display !== 'none' && style.visibility !== 'hidden'
                                  && node.getClientRects().length > 0;
                              };
                              return {
                                name: visible('candidate-name'), email: visible('candidate-email'),
                                collapsed: visible('collapsed-question'),
                                ssnHidden: Boolean(document.getElementById('hidden-ssn').closest('[hidden][inert][aria-hidden="true"]')),
                                csrfType: document.getElementById('csrf-token').type
                              };
                            }""")
                            self.assertTrue(visibility["name"])
                            self.assertTrue(visibility["email"])
                            self.assertFalse(visibility["collapsed"])
                            self.assertTrue(visibility["ssnHidden"])
                            self.assertEqual(visibility["csrfType"], "hidden")

                        elif fixture_name == "overlay-blocks-form.html":
                            page.locator("#candidate-email").evaluate(
                                "node => { node.value = 'SYNTHETIC-OVERLAY-ANSWER'; }"
                            )
                            cover = page.locator("#candidate-email").evaluate("""node => {
                              const rect = node.getBoundingClientRect();
                              const top = document.elementFromPoint(
                                rect.left + rect.width / 2, rect.top + rect.height / 2);
                              return Boolean(top && (top.id === 'consent-shade'
                                || top.closest('#consent-dialog')));
                            }""")
                            self.assertTrue(cover, "The rendered consent overlay must cover the form control")

                        elif fixture_name == "malformed-conditional-repeat.html":
                            page.locator("#sponsorship").select_option("yes")
                            duplicate_ids = page.locator("#conditional-fields #duplicate-id").count()
                            self.assertEqual(duplicate_ids, 2)
                            self.assertFalse(page.locator("#conditional-fields").is_hidden())
                            page.locator("#conditional-fields input").evaluate_all(
                                "nodes => nodes.forEach((node, i) => { node.value = 'SYNTHETIC-DYNAMIC-' + i; })"
                            )

                        _watch_form_events(page)
                        before = _snapshot(page)
                        report = analyze_form_page(page)
                        after = _snapshot(page)
                        serialized = json.dumps(report, sort_keys=True)

                        self.assertEqual(report["status"], "UNKNOWN", report)
                        self.assertEqual(report["reason"], "UNSAFE_TEXT_CONTEXT", report)
                        self.assertEqual(after, before, "DOM analysis must not mutate controls or submit")
                        self.assertEqual(after["events"]["submit"], 0)
                        self.assertNotIn("SYNTHETIC-HIDDEN-DECOY", serialized)
                        self.assertNotIn("SYNTHETIC-OVERLAY-ANSWER", serialized)
                        self.assertNotIn("SYNTHETIC-DYNAMIC-", serialized)
            finally:
                browser.close()

    def test_chromium_dom_projection_accepts_unlabelled_control_without_value_or_mutation(self):
        markup = """<!doctype html><form>
          <input id="contact-email" name="email" type="email" value="PRIVATE-CURRENT-VALUE">
        </form>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(markup, wait_until="load")
                _watch_form_events(page)
                before = _snapshot(page)

                report = analyze_form_page(page)

                self.assertEqual(report["status"], "PASS", report)
                self.assertEqual(report["fields"][0]["name"], "email")
                self.assertNotIn("PRIVATE-CURRENT-VALUE", json.dumps(report, sort_keys=True))
                self.assertEqual(_snapshot(page), before,
                                 "Positive DOM analysis must not change values or fire events")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
