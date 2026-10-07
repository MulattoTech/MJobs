import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cli.live_feedback import (
    Recorder,
    capture_redacted_screenshot,
    prune_expired_sessions,
    redact_sensitive_text,
    redact_url,
    run_session,
    safe_url,
    sanitize_interaction,
    text_fingerprint,
)


class LiveFeedbackTests(unittest.TestCase):
    def test_retention_removes_only_expired_feedback_sessions_and_repairs_latest(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "live-feedback"
            expired = root / "20260801T120000Z"
            boundary = root / "20260907T120000Z"
            recent = root / "20261001T120000Z"
            malformed = root / "20260801T120000Z-notes"
            for session in (expired, boundary, recent, malformed):
                session.mkdir(parents=True)
                (session / "marker.txt").write_text("synthetic", encoding="utf-8")
            (root / "LATEST.txt").write_text(expired.name + "\n", encoding="utf-8")
            application_history = Path(directory) / "applications" / "history.json"
            application_history.parent.mkdir()
            application_history.write_text("preserve", encoding="utf-8")

            removed = prune_expired_sessions(now=now, output_root=root)

            self.assertEqual(removed, [expired.name])
            self.assertFalse(expired.exists())
            self.assertTrue(boundary.is_dir())
            self.assertTrue(recent.is_dir())
            self.assertTrue(malformed.is_dir())
            self.assertEqual((root / "LATEST.txt").read_text(encoding="utf-8").strip(), recent.name)
            self.assertEqual(application_history.read_text(encoding="utf-8"), "preserve")

    def test_retention_is_idempotent_when_no_expired_managed_session_exists(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "live-feedback"
            (root / "20261001T120000Z").mkdir(parents=True)
            self.assertEqual(prune_expired_sessions(now=now, output_root=root), [])
            self.assertEqual(prune_expired_sessions(now=now, output_root=root), [])

    def test_new_feedback_session_automatically_prunes_old_diagnostics(self):
        from cli.live_feedback import new_session_directory

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "live-feedback"
            expired = root / "20200101T000000Z"
            expired.mkdir(parents=True)
            (expired / "trace.zip").write_bytes(b"synthetic old trace")
            with patch("cli.live_feedback.OUTPUT_ROOT", root):
                session = new_session_directory()
            self.assertTrue(session.is_dir())
            self.assertFalse(expired.exists())

    def test_visual_capture_masks_every_form_control_and_sensitive_region(self):
        class Page:
            def __init__(self):
                self.selectors = []
                self.capture = None

            def locator(self, selector):
                self.selectors.append(selector)
                return ("locator", selector)

            def evaluate(self, *args):
                return None

            def screenshot(self, **kwargs):
                self.capture = kwargs

        page = Page()
        capture_redacted_screenshot(page, "synthetic.png")

        self.assertEqual(page.selectors, [
            "input", "textarea", "select",
            "[contenteditable]:not([contenteditable='false'])",
            "[role='textbox']", "[role='combobox']", "[data-sensitive]",
            "[data-mjobs-sensitive-mask]",
        ])
        self.assertEqual(page.capture["path"], "synthetic.png")
        self.assertTrue(page.capture["full_page"])
        self.assertEqual(page.capture["mask"], [
            ("locator", "input"), ("locator", "textarea"),
            ("locator", "select"),
            ("locator", "[contenteditable]:not([contenteditable='false'])"),
            ("locator", "[role='textbox']"), ("locator", "[role='combobox']"),
            ("locator", "[data-sensitive]"),
            ("locator", "[data-mjobs-sensitive-mask]"),
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
            def evaluate(self, *args): return None
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

    def test_sensitive_text_is_removed_from_diagnostic_and_interaction_metadata(self):
        raw = "Contact dylan@example.test or bearer ghp_abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_sensitive_text(raw)
        self.assertNotIn("dylan@example.test", redacted)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertEqual(redact_sensitive_text("Gender: Female"), "[REDACTED]")
        self.assertEqual(redact_sensitive_text("token=synthetic-secret"), "[REDACTED]")

        result = sanitize_interaction({
            "type": "click", "tag": "button", "id": "dylan@example.test",
            "aria_label": "Email dylan@example.test",
            "text": "Contact dylan@example.test",
        })
        self.assertNotIn("dylan@example.test", str(result))
        sensitive_answer = sanitize_interaction({
            "type": "click", "tag": "button", "aria_label": "Gender: Female",
            "text": "Female",
        })
        self.assertEqual(sensitive_answer["text"], "[REDACTED]")
        self.assertEqual(sanitize_interaction({
            "type": "click", "tag": "button", "text": "Female",
            "sensitive_context": True,
        })["text"], "[REDACTED]")

    def test_diagnostic_text_flag_never_persists_raw_message(self):
        class Stream:
            def __init__(self): self.text = ""
            def write(self, value): self.text += value
            def flush(self): pass

        recorder = Recorder(Path("."), diagnostic_text=True)
        recorder.stream.close()
        recorder.stream = Stream()
        recorder.diagnostic_message("console", "error", "private dylan@example.test")
        self.assertNotIn("dylan@example.test", recorder.stream.text)
        self.assertNotIn('"text"', recorder.stream.text)


if __name__ == "__main__":
    unittest.main()
