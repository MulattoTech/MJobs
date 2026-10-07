"""Visual feedback screenshots mask synthetic sensitive browser controls."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright

from cli.live_feedback import capture_redacted_screenshot


class LiveFeedbackScreenshotRedactionTests(unittest.TestCase):
    def test_synthetic_password_demographic_and_sensitive_regions_are_masked(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 400})
                page.set_content("""
                    <style>body { font: 24px sans-serif; } input, select { display: block; margin: 20px; }</style>
                    <input aria-label="Password" type="password" value="SYNTHETIC_PASSWORD_CANARY">
                    <select aria-label="Sensitive demographic answer">
                      <option selected>SYNTHETIC_DEMOGRAPHIC_CANARY</option>
                    </select>
                    <p data-sensitive>SYNTHETIC_TOKEN_CANARY</p>
                    <p>Contact dylan@example.test</p>
                    <div id="gender-answer"><span>Gender</span><span>Female</span></div>
                    <p>Public synthetic heading</p>
                """)
                with tempfile.TemporaryDirectory() as directory:
                    capture_path = Path(directory) / "capture.png"
                    boxes = [
                        page.get_by_label("Password").bounding_box(),
                        page.get_by_label("Sensitive demographic answer").bounding_box(),
                        page.locator("[data-sensitive]").bounding_box(),
                        page.get_by_text("Contact dylan@example.test").bounding_box(),
                        page.locator("#gender-answer").bounding_box(),
                    ]
                    capture_redacted_screenshot(page, capture_path)
                    self.assertGreaterEqual(len(boxes), 5)
                    with Image.open(capture_path) as image:
                        pixels = image.convert("RGB")
                        for box in boxes:
                            self.assertIsNotNone(box)
                            sample = (int(box["x"] + box["width"] / 2),
                                      int(box["y"] + box["height"] / 2))
                            self.assertEqual(pixels.getpixel(sample), (0, 0, 0))
            finally:
                browser.close()

    def test_synthetic_rich_text_answers_are_masked(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 320})
                page.set_content("""
                    <style>
                      body { font: 24px sans-serif; }
                      [contenteditable], [role=textbox], [role=combobox] {
                        display: block; margin: 24px; min-height: 32px;
                      }
                    </style>
                    <div contenteditable="plaintext-only" aria-label="Answer">SYNTHETIC_WORK_AUTHORIZATION_ANSWER</div>
                    <div role="textbox" aria-label="Answer">SYNTHETIC_DEMOGRAPHIC_ANSWER</div>
                    <div role="combobox" aria-label="Answer">SYNTHETIC_VISA_ANSWER</div>
                """)
                boxes = [
                    page.locator("[contenteditable]").bounding_box(),
                    page.locator("[role='textbox']").bounding_box(),
                    page.locator("[role='combobox']").bounding_box(),
                ]
                with tempfile.TemporaryDirectory() as directory:
                    capture_path = Path(directory) / "rich-controls.png"
                    capture_redacted_screenshot(page, capture_path)
                    with Image.open(capture_path) as image:
                        pixels = image.convert("RGB")
                        for box in boxes:
                            self.assertIsNotNone(box)
                            sample = (int(box["x"] + box["width"] / 2),
                                      int(box["y"] + box["height"] / 2))
                            self.assertEqual(pixels.getpixel(sample), (0, 0, 0))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
