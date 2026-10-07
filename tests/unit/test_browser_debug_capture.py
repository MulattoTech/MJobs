import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.applications.debug_capture import (
    append_diagnostic_event,
    append_console_error,
    capture_browser_failure,
    prune_debug_captures,
    sanitize_debug_url,
)


class BrowserDebugCaptureTests(unittest.TestCase):
    def test_failure_capture_contains_only_bounded_structural_and_hashed_data(self):
        class Page:
            url = "http://user:password@127.0.0.1:8090/ats/apply/private-id?token=secret#answer"

            def __init__(self):
                self.screenshots = []

            def evaluate(self, script):
                if "document.documentElement" in script:
                    return {
                        "element_counts": {"form": 1, "input": 3, "PRIVATE@example.test": 99},
                        "role_counts": {"textbox": 2},
                        "text": "PRIVATE_PAGE_TEXT",
                    }
                return None

            def locator(self, selector):
                return selector

            def screenshot(self, **kwargs):
                self.screenshots.append(kwargs)
                Path(kwargs["path"]).write_bytes(b"synthetic-redacted-png")

        now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
        page = Page()
        events = []
        append_diagnostic_event(events, "console", page.url, "PRIVATE_CONSOLE_MESSAGE")
        append_diagnostic_event(events, "requestfailed", page.url, "PRIVATE_NETWORK_ERROR")
        with tempfile.TemporaryDirectory() as directory:
            capture = capture_browser_failure(
                page,
                Path(directory) / "debug-captures",
                attempt_id="a" * 32,
                step="DETECT_FORM",
                configured_url=page.url,
                error=RuntimeError("PRIVATE_EXCEPTION_MESSAGE"),
                events=events,
                now=now,
            )

            self.assertIsNotNone(capture)
            metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
            dom = json.loads((capture / "dom.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["current_step"], "DETECT_FORM")
            self.assertEqual(metadata["current_url"], "http://127.0.0.1:8090/:redacted/:redacted/:redacted")
            self.assertEqual(metadata["error"]["type"], "RuntimeError")
            self.assertNotIn("message", metadata["error"])
            self.assertEqual(len(metadata["diagnostics"]), 2)
            self.assertTrue(all("message_fingerprint" in event for event in metadata["diagnostics"]))
            self.assertTrue(all("message_sha256" not in event for event in metadata["diagnostics"]))
            self.assertEqual(dom["element_counts"], {"form": 1, "input": 3})
            self.assertEqual(dom["role_counts"], {"textbox": 2})
            self.assertFalse((capture / "screenshot.png").read_bytes().find(b"PRIVATE") >= 0)
            serialized = (capture / "metadata.json").read_text(encoding="utf-8")
            serialized += (capture / "dom.json").read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE", serialized)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("message_sha256", serialized)
            self.assertIn("message_fingerprint", metadata["error"])
            self.assertTrue((capture / "screenshot.png").is_file())
            self.assertEqual(page.screenshots[0]["full_page"], False)
            self.assertLessEqual(page.screenshots[0]["timeout"], 2000)

    def test_debug_url_drops_credentials_query_fragment_and_identifiers(self):
        self.assertEqual(
            sanitize_debug_url("https://user:pass@example.test/jobs/secret-id?token=private#section"),
            "https://example.test/:redacted/:redacted",
        )
        self.assertEqual(sanitize_debug_url("file:///private/resume.pdf"), "")

    def test_console_capture_ignores_non_error_messages(self):
        class ConsoleMessage:
            def __init__(self, message_type, text):
                self.type = message_type
                self.text = text

        events = []
        append_console_error(events, ConsoleMessage("log", "PRIVATE_NORMAL_LOG"), "https://example.test")
        append_console_error(events, ConsoleMessage("error", "PRIVATE_CONSOLE_ERROR"), "https://example.test")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "console")
        self.assertNotIn("PRIVATE", json.dumps(events))

    def test_event_buffer_and_capture_retention_are_bounded(self):
        now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
        events = []
        for index in range(75):
            append_diagnostic_event(events, "console", "https://example.test/form?token=x", str(index))
        self.assertEqual(len(events), 50)
        self.assertNotIn("token=x", json.dumps(events))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "debug-captures"
            root.mkdir()
            for index in range(25):
                child = root / f"{index:032x}"
                child.mkdir()
                (child / "marker").write_text("synthetic", encoding="utf-8")
                timestamp = (now - timedelta(days=40) if index == 0
                             else now - timedelta(minutes=25-index))
                epoch = timestamp.timestamp()
                os.utime(child, (epoch, epoch))
            unknown = root / "manual-notes"
            unknown.mkdir()
            (unknown / "keep.txt").write_text("preserve", encoding="utf-8")

            removed = prune_debug_captures(root, now=now)

            self.assertIn("00000000000000000000000000000000", removed)
            self.assertEqual(len([path for path in root.iterdir() if path.name != "manual-notes"]), 20)
            self.assertEqual((unknown / "keep.txt").read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
