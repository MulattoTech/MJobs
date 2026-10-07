"""Visual feedback screenshots mask synthetic sensitive browser controls."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright

from cli.live_feedback import capture_redacted_screenshot


class LiveFeedbackScreenshotRedactionTests(unittest.TestCase):
    def test_arbitrary_text_and_embedded_media_are_hidden_without_changing_page(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 400})
                page.set_content("""
                    <style>
                      body { margin: 24px; background: #fff; color: #000; font: 30px Arial; }
                      #canvas-private { display: block; width: 500px; height: 90px; }
                      #generated-private::before { content: "SYNTHETIC_GENERATED_PRIVATE_7391"; }
                    </style>
                    <p id="ordinary-private">SYNTHETIC_UNCLASSIFIED_ANSWER_8426</p>
                    <p id="generated-private">Visible body text</p>
                    <canvas id="canvas-private" width="500" height="90"></canvas>
                    <img id="image-private" alt="synthetic image"
                      src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='90' height='60'%3E%3Crect width='90' height='60' fill='black'/%3E%3C/svg%3E">
                    <script>
                      const canvas = document.querySelector('#canvas-private');
                      const context = canvas.getContext('2d');
                      context.fillStyle = '#000';
                      context.font = '30px Arial';
                      context.fillText('SYNTHETIC_CANVAS_ANSWER_1964', 4, 48);
                    </script>
                """)
                ordinary = page.locator("#ordinary-private").bounding_box()
                generated = page.locator("#generated-private").bounding_box()
                canvas = page.locator("#canvas-private").bounding_box()
                image = page.locator("#image-private")
                image.evaluate("image => image.decode()")
                image_box = image.bounding_box()

                with tempfile.TemporaryDirectory() as directory:
                    baseline_path = Path(directory) / "baseline.png"
                    capture_path = Path(directory) / "redacted.png"
                    page.screenshot(path=str(baseline_path), full_page=True)
                    capture_redacted_screenshot(page, capture_path)

                    with Image.open(baseline_path) as image:
                        baseline = image.convert("RGB")
                        self.assertGreater(self._dark_pixel_count(baseline, ordinary), 0)
                        self.assertGreater(self._dark_pixel_count(baseline, generated), 0)
                        self.assertGreater(self._dark_pixel_count(baseline, canvas), 0)
                        self.assertGreater(self._dark_pixel_count(baseline, image_box), 0)
                    with Image.open(capture_path) as image:
                        redacted = image.convert("RGB")
                        self.assertEqual(self._dark_pixel_count(redacted, ordinary), 0)
                        self.assertEqual(self._dark_pixel_count(redacted, generated), 0)
                        self.assertEqual(self._dark_pixel_count(redacted, canvas), 0)
                        self.assertEqual(self._dark_pixel_count(redacted, image_box), 0)

                self.assertEqual(
                    page.locator("#ordinary-private").inner_text(),
                    "SYNTHETIC_UNCLASSIFIED_ANSWER_8426",
                )
                self.assertEqual(
                    page.locator("#canvas-private").evaluate(
                        "canvas => canvas.toDataURL().length"
                    ) > 100,
                    True,
                )
            finally:
                browser.close()

    @staticmethod
    def _dark_pixel_count(image, box):
        left = max(0, int(box["x"]))
        top = max(0, int(box["y"]))
        right = min(image.width, int(box["x"] + box["width"]) + 1)
        bottom = min(image.height, int(box["y"] + box["height"]) + 1)
        crop = image.crop((left, top, right, bottom))
        return sum(1 for red, green, blue in crop.get_flattened_data()
                   if max(red, green, blue) < 100)

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
