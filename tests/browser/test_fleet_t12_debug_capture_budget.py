"""Bounded failure-capture budget and privacy regressions (synthetic pages only)."""
from concurrent.futures import CancelledError
import base64
import json
from pathlib import Path
import tempfile
import time
import unittest

from services.applications.debug_capture import capture_browser_failure


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0N8AAAAASUVORK5CYII="
)


class _BudgetCheck:
    """Small caller-budget oracle with cooperative cancellation."""
    def __init__(self, seconds, *, cancelled=False):
        self.started = time.monotonic()
        self.seconds = seconds
        self.cancelled = cancelled
        self.calls = 0
        self.remaining_calls = 0

    def _remaining(self):
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    def remaining_seconds(self):
        self.remaining_calls += 1
        return self._remaining()

    def __call__(self):
        self.calls += 1
        if self.cancelled:
            raise CancelledError("synthetic cancellation")
        if self._remaining() <= 0:
            raise TimeoutError("synthetic caller deadline exhausted")


class _SyntheticPage:
    url = "https://example.test/jobs/private-id?token=SYNTHETIC_URL_SECRET"

    def __init__(self, screenshot):
        self._screenshot = screenshot
        self.evaluate_calls = 0
        self.locator_calls = []
        self.screenshot_calls = []

    def evaluate(self, _script):
        self.evaluate_calls += 1
        raise AssertionError("unbounded standalone DOM evaluation is forbidden")

    def locator(self, selector):
        self.locator_calls.append(selector)
        return selector

    def screenshot(self, **options):
        self.screenshot_calls.append(options)
        return self._screenshot(options)


class DebugCaptureBudgetTests(unittest.TestCase):
    def capture(self, root, page, check, error, *, attempt_id="a" * 32):
        return capture_browser_failure(
            page, root, attempt_id=attempt_id, step="DETECT_FORM",
            configured_url=page.url, error=error, check=check,
        )

    def test_renderer_stall_respects_one_shared_capped_budget_and_leaves_no_stale_metadata(self):
        check = _BudgetCheck(0.08)

        def stalled_screenshot(options):
            timeout_ms = options["timeout"]
            self.assertGreater(timeout_ms, 0)
            self.assertLessEqual(timeout_ms, 2000)
            self.assertLessEqual(timeout_ms, 80)
            Path(options["path"]).write_bytes(b"partial synthetic PNG")
            time.sleep(timeout_ms / 1000)
            raise TimeoutError("synthetic renderer stall")

        page = _SyntheticPage(stalled_screenshot)
        original_error = RuntimeError("SYNTHETIC_ORIGINAL_FAILURE")
        with tempfile.TemporaryDirectory(prefix="mjobs-t12-stall-") as directory:
            root = Path(directory) / "captures"
            started = time.monotonic()
            result = self.capture(root, page, check, original_error)
            elapsed = time.monotonic() - started

            self.assertIsNone(result)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(len(page.screenshot_calls), 1)
            self.assertEqual(page.evaluate_calls, 0)
            self.assertGreaterEqual(check.calls, 2)
            self.assertEqual(check.remaining_calls, 1)
            self.assertFalse((root / ("a" * 32) / "metadata.json").exists())

    def test_healthy_capture_reads_back_png_and_privacy_safe_metadata(self):
        check = _BudgetCheck(5.0)
        page = _SyntheticPage(lambda options: Path(options["path"]).write_bytes(_PNG))
        error = RuntimeError("SYNTHETIC_ORIGINAL_FAILURE")
        with tempfile.TemporaryDirectory(prefix="mjobs-t12-healthy-") as directory:
            result = self.capture(Path(directory) / "captures", page, check, error)

            self.assertIsNotNone(result)
            screenshot = result / "screenshot.png"
            metadata_path = result / "metadata.json"
            dom_path = result / "dom.json"
            self.assertEqual(screenshot.read_bytes(), _PNG)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            dom = json.loads(dom_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["screenshot"], {
                "file": "screenshot.png", "status": "CAPTURED"})
            self.assertEqual(dom["status"], "OMITTED_UNBOUNDED_EVALUATION")
            self.assertEqual(dom["element_counts"], {})
            self.assertEqual(dom["role_counts"], {})
            self.assertTrue(dom["text_omitted"])
            self.assertTrue(dom["attributes_omitted"])
            self.assertTrue(dom["control_values_omitted"])
            self.assertEqual(page.evaluate_calls, 0)
            self.assertLessEqual(page.screenshot_calls[0]["timeout"], 2000)
            self.assertGreaterEqual(check.calls, 3)
            self.assertEqual(check.remaining_calls, 1)
            serialized = metadata_path.read_text(encoding="utf-8") + dom_path.read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC_URL_SECRET", serialized)
            self.assertNotIn("SYNTHETIC_ORIGINAL_FAILURE", serialized)

    def test_expired_and_precancelled_checks_do_no_browser_work(self):
        cases = (("expired", _BudgetCheck(0)),
                 ("cancelled", _BudgetCheck(5, cancelled=True)))
        for label, check in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix=f"mjobs-t12-{label}-") as directory:
                page = _SyntheticPage(lambda _options: self.fail("screenshot must not run"))
                result = self.capture(Path(directory) / "captures", page, check,
                                      RuntimeError("SYNTHETIC_FAILURE"))
                self.assertIsNone(result)
                self.assertEqual(page.evaluate_calls, 0)
                self.assertEqual(page.locator_calls, [])
                self.assertEqual(page.screenshot_calls, [])
                self.assertEqual(check.calls, 1)

    def test_screenshot_error_does_not_replace_original_failure_or_leak_values(self):
        check = _BudgetCheck(5.0)
        screenshot_error = "SYNTHETIC_SCREENSHOT_PRIVATE_VALUE"
        original_error = ValueError("SYNTHETIC_ORIGINAL_PRIVATE_VALUE")
        page = _SyntheticPage(lambda _options: (_ for _ in ()).throw(
            RuntimeError(screenshot_error)))
        with tempfile.TemporaryDirectory(prefix="mjobs-t12-error-") as directory:
            result = self.capture(Path(directory) / "captures", page, check, original_error)

            self.assertIsNotNone(result)
            metadata_path = result / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["error"]["type"], "ValueError")
            self.assertEqual(metadata["error"]["message_length"],
                             len("SYNTHETIC_ORIGINAL_PRIVATE_VALUE"))
            self.assertEqual(metadata["screenshot"], {"file": None, "status": "FAILED"})
            serialized = metadata_path.read_text(encoding="utf-8")
            serialized += (result / "dom.json").read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC_ORIGINAL_PRIVATE_VALUE", serialized)
            self.assertNotIn(screenshot_error, serialized)
            self.assertEqual(str(original_error), "SYNTHETIC_ORIGINAL_PRIVATE_VALUE")
            self.assertEqual(page.evaluate_calls, 0)
            self.assertGreaterEqual(check.calls, 3)


if __name__ == "__main__":
    unittest.main()
