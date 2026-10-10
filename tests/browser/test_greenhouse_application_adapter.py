"""Synthetic browser contract for the review-first Greenhouse adapter."""
import hashlib
import unittest

from playwright.sync_api import sync_playwright

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.upload_integrity import snapshot_bytes


class GreenhouseApplicationAdapterBrowserTests(unittest.TestCase):
    def test_uploads_only_verified_documents_to_a_local_unambiguous_fixture(self):
        html = """<!doctype html><html><body><form id="application">
          <input id="candidate-name" name="name" aria-label="Name" required>
          <input id="resume-file" name="resume" aria-label="Resume" type="file" required>
          <input id="cover-letter-file" name="cover_letter" aria-label="Cover letter" type="file">
          <button type="submit">Submit application</button>
        </form><script>
          window.mjobsActions = {submits: 0};
          document.querySelector('#application').addEventListener('submit', event => {
            event.preventDefault(); window.mjobsActions.submits++;
          });
        </script></body></html>"""
        resume_bytes = b"%PDF-1.4\nsynthetic resume\n"
        letter_bytes = b"%PDF-1.4\nsynthetic cover letter\n"
        snapshots = [
            snapshot_bytes("resume", "resume-1", "resume.pdf", "application/pdf",
                           resume_bytes, hashlib.sha256(resume_bytes).hexdigest()),
            snapshot_bytes("cover-letter", "letter-1", "letter.pdf", "application/pdf",
                           letter_bytes, hashlib.sha256(letter_bytes).hexdigest()),
        ]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route("http://127.0.0.1:8765/**", lambda route: route.fulfill(
                status=200, content_type="text/html", body=html))
            page.goto("http://127.0.0.1:8765/fixture")
            adapter = GreenhouseApplicationAdapter()
            analysis = adapter.analyze_page(page)

            manifest = adapter.upload_mock_fixture_documents(page, analysis, snapshots)

            self.assertEqual(manifest, [
                {"kind": "resume", "document_id": "resume-1", "size_bytes": len(resume_bytes),
                 "sha256": hashlib.sha256(resume_bytes).hexdigest()},
                {"kind": "cover-letter", "document_id": "letter-1", "size_bytes": len(letter_bytes),
                 "sha256": hashlib.sha256(letter_bytes).hexdigest()},
            ])
            self.assertEqual(page.locator("input[type=file]").evaluate_all(
                "nodes => nodes.map(node => Array.from(node.files).map(file => file.name))"),
                [["resume.pdf"], ["letter.pdf"]])
            self.assertEqual(page.evaluate("window.mjobsActions.submits"), 0)
            browser.close()

    def test_uncertain_form_and_nonlocal_origin_block_fixture_upload(self):
        html = """<!doctype html><html><body><form>
          <input id="resume-file" name="resume" aria-label="Resume" type="file">
          <input id="gender" name="gender" aria-label="Gender">
          <input id="cover-letter-file" name="cover_letter" aria-label="Cover letter" type="file">
        </form></body></html>"""
        resume_bytes = b"%PDF-1.4\nsynthetic resume\n"
        letter_bytes = b"%PDF-1.4\nsynthetic cover letter\n"
        snapshots = [
            snapshot_bytes("resume", "resume-1", "resume.pdf", "application/pdf",
                           resume_bytes, hashlib.sha256(resume_bytes).hexdigest()),
            snapshot_bytes("cover-letter", "letter-1", "letter.pdf", "application/pdf",
                           letter_bytes, hashlib.sha256(letter_bytes).hexdigest()),
        ]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route("http://127.0.0.1:8765/**", lambda route: route.fulfill(
                status=200, content_type="text/html", body=html))
            page.goto("http://127.0.0.1:8765/fixture")
            adapter = GreenhouseApplicationAdapter()
            analysis = adapter.analyze_page(page)
            with self.assertRaisesRegex(ValueError, "GREENHOUSE_FORM_REQUIRES_HUMAN_REVIEW"):
                adapter.upload_mock_fixture_documents(page, analysis, snapshots)
            self.assertEqual(page.locator("input[type=file]").evaluate_all(
                "nodes => nodes.map(node => node.files.length)"), [0, 0])

            page.route("https://boards.greenhouse.io/**", lambda route: route.fulfill(
                status=200, content_type="text/html", body=html))
            page.goto("https://boards.greenhouse.io/example/jobs/1234")
            analysis = adapter.analyze_page(page)
            with self.assertRaisesRegex(ValueError, "GREENHOUSE_FIXTURE_ORIGIN_REQUIRED"):
                adapter.upload_mock_fixture_documents(page, analysis, snapshots)
            browser.close()

    def test_extracts_safe_form_metadata_and_leaves_application_untouched(self):
        html = """<!doctype html>
<html><body>
  <form id="application" action="/applications/submit" method="post">
    <label for="first-name"></label>
    <input id="first-name" name="first_name" aria-label="First name" autocomplete="given-name"
           value="PRIVATE-DECOY-FIRST" required>
    <label for="email"></label>
    <input id="email" name="email" aria-label="Email" type="email" value="private@example.invalid" required>
    <label for="gender"></label>
    <select id="gender" name="gender" aria-label="Gender" required>
      <option value="">Choose</option>
    <option value="PRIVATE-DECOY-OPTION">Self-identification</option>
    </select>
    <label for="resume"></label>
    <input id="resume" name="resume" aria-label="Resume" type="file" accept=".pdf,.doc,.docx" required>
    <label for="cover-letter"></label>
    <input id="cover-letter" name="cover_letter" aria-label="Cover letter" type="file" accept=".pdf,.doc,.docx">
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
