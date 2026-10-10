"""Synthetic browser contract for the review-first Greenhouse adapter."""
import unittest

from playwright.sync_api import sync_playwright

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter


class GreenhouseApplicationAdapterBrowserTests(unittest.TestCase):
    def test_extracts_safe_form_metadata_and_leaves_application_untouched(self):
        html = """<!doctype html>
<html><body>
  <form id="application" action="/applications/submit" method="post">
    <label for="first-name">First name</label>
    <input id="first-name" name="first_name" autocomplete="given-name"
           value="PRIVATE-DECOY-FIRST" required>
    <label for="email">Email</label>
    <input id="email" name="email" type="email" value="private@example.invalid" required>
    <label for="gender">Gender</label>
    <select id="gender" name="gender" required>
      <option value="">Choose</option>
    <option value="PRIVATE-DECOY-OPTION">Self-identification</option>
    </select>
    <label for="resume">Resume</label>
    <input id="resume" name="resume" type="file" accept=".pdf,.doc,.docx" required>
    <label for="cover-letter">Cover letter</label>
    <input id="cover-letter" name="cover_letter" type="file" accept=".pdf,.doc,.docx">
    <button id="submit-application" type="submit">Submit application</button>
  </form>
  <output id="activity"></output>
  <script>
    window.mjobsActions = {clicks: 0, submits: 0, changes: 0};
    document.addEventListener('click', () => window.mjobsActions.clicks++);
    document.querySelector('#application').addEventListener('submit', event => {
      event.preventDefault(); window.mjobsActions.submits++;
    });
    document.addEventListener('change', () => window.mjobsActions.changes++);
  </script>
</body></html>"""

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)

            result = GreenhouseApplicationAdapter().analyze_page(page)

            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("status"), "PASS")
            fields = result.get("fields")
            self.assertIsInstance(fields, (list, dict))
            rows = list(fields.values()) if isinstance(fields, dict) else fields
            self.assertTrue(rows, "Expected form controls to be recognized")

            serialized = repr(result).casefold()
            for decoy in ("private-decoy-first", "private@example.invalid", "private-decoy-option"):
                self.assertNotIn(decoy, serialized, "Form values must never leave the page")

            def matching_field(*needles):
                for row in rows:
                    metadata = repr(row).casefold()
                    if all(needle in metadata for needle in needles):
                        return row
                self.fail(f"No safe field metadata matched {needles!r}: {rows!r}")

            first_name = matching_field("first_name")
            self.assertEqual(first_name.get("input_type"), "text")

            sensitive = matching_field("gender")
            self.assertEqual(sensitive.get("sensitivity"), "SENSITIVE")
            adapter = GreenhouseApplicationAdapter()
            self.assertTrue(adapter.requires_human_review(result))

            resume = matching_field("resume")
            cover_letter = matching_field("cover_letter")
            self.assertIn(str(resume.get("input_type", resume.get("type", ""))).casefold(), {"file", "upload"})
            self.assertIn(str(cover_letter.get("input_type", cover_letter.get("type", ""))).casefold(), {"file", "upload"})

            capabilities = adapter.capabilities()
            self.assertFalse(capabilities.get("submission"))
            self.assertFalse(capabilities.get("live_form_interaction"))

            self.assertEqual(page.evaluate("window.mjobsActions"), {
                "clicks": 0, "submits": 0, "changes": 0,
            })
            self.assertEqual(page.locator("input[type=file]").evaluate_all(
                "nodes => nodes.map(node => node.files.length)"), [0, 0])
            self.assertEqual(page.locator("#first-name").input_value(), "PRIVATE-DECOY-FIRST")
            browser.close()


if __name__ == "__main__":
    unittest.main()
