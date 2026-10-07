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
                    <p>Public synthetic heading</p>
                """)
                with tempfile.TemporaryDirectory() as directory:
                    capture_path = Path(directory) / "capture.png"
                    boxes = [
                        page.get_by_label("Password").bounding_box(),
                        page.get_by_label("Sensitive demographic answer").bounding_box(),
                        page.locator("[data-sensitive]").bounding_box(),
                    ]
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
