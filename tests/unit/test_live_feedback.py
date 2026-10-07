import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.live_feedback import (
    capture_redacted_screenshot,
    redact_url,
    run_session,
    safe_url,
    sanitize_interaction,
    text_fingerprint,
)


class LiveFeedbackTests(unittest.TestCase):
    def test_visual_capture_masks_every_form_control_and_sensitive_region(self):
        class Page:
            def __init__(self):
                self.selectors = []
                self.capture = None

            def locator(self, selector):
                self.selectors.append(selector)
                return ("locator", selector)

            def screenshot(self, **kwargs):
                self.capture = kwargs

        page = Page()
        capture_redacted_screenshot(page, "synthetic.png")

        self.assertEqual(page.selectors, ["input", "textarea", "select", "[data-sensitive]"])
        self.assertEqual(page.capture["path"], "synthetic.png")
        self.assertTrue(page.capture["full_page"])
        self.assertEqual(page.capture["mask"], [
            ("locator", "input"), ("locator", "textarea"),
            ("locator", "select"), ("locator", "[data-sensitive]"),
        ])
        self.assertEqual(page.capture["mask_color"], "#000000")

    def test_visual_feedback_does_not_write_raw_dom_trace(self):
        class Tracing:
            def start(self, **kwargs):
                raise AssertionError("raw Playwright tracing must remain disabled")

            def stop(self, **kwargs):
                raise AssertionError("raw Playwright tracing must remain disabled")

        class Page:
            url = "http://127.0.0.1:18700/jobs?token=synthetic"

            def expose_function(self, *args): pass
            def add_init_script(self, *args): pass
            def on(self, *args): pass
            def goto(self, *args, **kwargs): pass
            def locator(self, selector): return ("locator", selector)
            def title(self): return "Synthetic feedback page"
            def screenshot(self, **kwargs):
                with open(kwargs["path"], "wb") as output:
                    output.write(b"synthetic masked screenshot")

        class Context:
            tracing = Tracing()

            def new_page(self): return Page()

        class Browser:
            def new_context(self): return Context()
            def close(self): pass

        class Chromium:
            def launch(self, **kwargs): return Browser()

        class Playwright:
            chromium = Chromium()

        class PlaywrightManager:
            def __enter__(self): return Playwright()
            def __exit__(self, *args): pass

        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "live-feedback"
            with patch("cli.live_feedback.OUTPUT_ROOT", output_root), \
                 patch("cli.live_feedback.sync_playwright", return_value=PlaywrightManager()), \
                 patch("builtins.input", return_value=""):
                session_dir = run_session("http://127.0.0.1:18700", visuals=True)

            self.assertTrue((session_dir / "final.png").is_file())
            self.assertFalse((session_dir / "trace.zip").exists())
            session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(session["final_url"], "http://127.0.0.1:18700/jobs")

    def test_safe_url_requires_local_http_and_removes_query_fragment(self):
        self.assertEqual(
            safe_url("http://127.0.0.1:18701/jobs?token=secret#section"),
            "http://127.0.0.1:18701/jobs",
        )
        with self.assertRaises(ValueError):
            safe_url("https://example.com/jobs?token=secret")

    def test_redact_url_never_preserves_query_or_fragment(self):
        self.assertEqual(
            redact_url("https://example.com/apply?email=user@example.com#private"),
            "https://example.com/apply",
        )

    def test_form_interaction_never_records_value_or_text(self):
        payload = {
            "type": "change",
            "tag": "input",
            "id": "email",
            "name": "email",
            "role": "textbox",
            "aria_label": "Email",
            "text": "dylan@example.com",
            "value": "dylan@example.com",
            "password": "secret",
        }
        result = sanitize_interaction(payload)
        self.assertNotIn("value", result)
        self.assertNotIn("password", result)
        self.assertNotIn("text", result)
        self.assertEqual(result["name"], "email")

    def test_non_form_click_keeps_only_bounded_element_metadata(self):
        result = sanitize_interaction({
            "type": "click", "tag": "button", "id": "search", "name": "",
            "role": "button", "aria_label": "Search jobs",
            "text": "Search " + ("x" * 500), "value": "must-not-appear",
        })
        self.assertEqual(result["tag"], "button")
        self.assertLessEqual(len(result["text"]), 160)
        self.assertNotIn("value", result)

    def test_diagnostic_text_fingerprint_does_not_expose_original_text(self):
        result = text_fingerprint("private diagnostic string")
        self.assertEqual(result["length"], len("private diagnostic string"))
        self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
