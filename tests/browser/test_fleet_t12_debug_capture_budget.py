"""Native Chromium debug-capture budget and privacy regressions."""
from concurrent.futures import CancelledError
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

from services.applications.debug_capture import capture_browser_failure


_HTML = """<!doctype html><html><body><main><h1>Synthetic fixture</h1>
<input name="synthetic" value="SYNTHETIC_PRIVATE_VALUE"></main></body></html>"""


class _BudgetCheck:
    """Caller-owned deadline and cooperative cancellation oracle."""
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


class DebugCaptureBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_content(_HTML)

    def tearDown(self):
        self.context.close()

    @staticmethod
    def capture(page, root, attempt_id, check, error):
        return capture_browser_failure(
            page, root, attempt_id=attempt_id, step="DETECT_FORM",
            configured_url="https://fixture.invalid/apply/synthetic",
            error=error, check=check,
        )

    @staticmethod
    def assert_dom_omitted(test, capture_path):
        dom = json.loads((capture_path / "dom.json").read_text(encoding="utf-8"))
        test.assertEqual(dom["status"], "OMITTED_UNBOUNDED_EVALUATION")
        test.assertEqual(dom["element_counts"], {})
        test.assertEqual(dom["role_counts"], {})
        test.assertTrue(dom["text_omitted"])
        test.assertTrue(dom["attributes_omitted"])
        test.assertTrue(dom["control_values_omitted"])

    def test_real_renderer_stall_is_bounded_then_independent_capture_reads_back(self):
        # Schedule, but do not wait for, a finite renderer stall. This test-only
        # evaluate happens before the diagnostic evaluate prohibition is armed.
        self.page.evaluate("""() => setTimeout(() => {
          const end = performance.now() + 1000;
          while (performance.now() < end) {}
        }, 0)""")
        check = _BudgetCheck(0.12)
        original_screenshot = self.page.screenshot
        observed_timeouts = []

        def capture_screenshot(*args, **kwargs):
            observed_timeouts.append(kwargs["timeout"])
            return original_screenshot(*args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="mjobs-t12-renderer-stall-") as directory:
            root = Path(directory) / "captures"
            started = time.monotonic()
            with patch.object(self.page, "evaluate", side_effect=AssertionError(
                    "diagnostic capture must not evaluate DOM")) as diagnostic_evaluate, \
                    patch.object(self.page, "screenshot", side_effect=capture_screenshot):
                stalled = self.capture(self.page, root, "a" * 32, check,
                                       RuntimeError("SYNTHETIC_ORIGINAL_FAILURE"))
            elapsed = time.monotonic() - started

            self.assertIsNone(stalled)
            self.assertLess(elapsed, 0.75)
            self.assertEqual(len(observed_timeouts), 1)
            self.assertGreater(observed_timeouts[0], 0)
            self.assertLessEqual(observed_timeouts[0], 120)
            diagnostic_evaluate.assert_not_called()
            self.assertEqual(check.remaining_calls, 1)
            self.assertGreaterEqual(check.calls, 2)
            self.assertFalse((root / ("a" * 32) / "metadata.json").exists())

            # Let the scheduled renderer work finish, then verify a separate
            # synthetic page can produce a complete package and durable readback.
            time.sleep(max(0, 1.0 - elapsed) + 0.05)
            healthy_context = self.browser.new_context()
            try:
                healthy_page = healthy_context.new_page()
                healthy_page.set_content(_HTML)
                healthy_check = _BudgetCheck(5.0)
                with patch.object(healthy_page, "evaluate", side_effect=AssertionError(
                        "diagnostic capture must not evaluate DOM")) as healthy_evaluate:
                    healthy = self.capture(healthy_page, root, "b" * 32, healthy_check,
                                           ValueError("SYNTHETIC_PRIMARY_ERROR"))
                healthy_evaluate.assert_not_called()

                self.assertIsNotNone(healthy)
                screenshot = healthy / "screenshot.png"
                with Image.open(screenshot) as image:
                    image.verify()
                metadata = json.loads((healthy / "metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["screenshot"], {
                    "file": "screenshot.png", "status": "CAPTURED"})
                self.assert_dom_omitted(self, healthy)
                self.assertGreaterEqual(healthy_check.calls, 3)
                self.assertEqual(healthy_check.remaining_calls, 1)
                serialized = ((healthy / "metadata.json").read_text(encoding="utf-8")
                              + (healthy / "dom.json").read_text(encoding="utf-8"))
                self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", serialized)
                self.assertNotIn("SYNTHETIC_PRIMARY_ERROR", serialized)
            finally:
                healthy_context.close()

    def test_expired_and_precancelled_checks_do_no_browser_work(self):
        cases = (("expired", _BudgetCheck(0)),
                 ("cancelled", _BudgetCheck(5, cancelled=True)))
        for index, (label, check) in enumerate(cases):
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix=f"mjobs-t12-{label}-") as directory:
                page = self.context.new_page()
                page.set_content(_HTML)
                with patch.object(page, "evaluate", side_effect=AssertionError(
                        "expired capture must not evaluate")) as evaluate, \
                        patch.object(page, "locator", side_effect=AssertionError(
                            "expired capture must not inspect locators")) as locator, \
                        patch.object(page, "screenshot", side_effect=AssertionError(
                            "expired capture must not screenshot")) as screenshot:
                    result = self.capture(page, Path(directory) / "captures",
                                          f"{index:032x}", check,
                                          RuntimeError("SYNTHETIC_FAILURE"))
                self.assertIsNone(result)
                evaluate.assert_not_called()
                locator.assert_not_called()
                screenshot.assert_not_called()
                self.assertEqual(check.calls, 1)
                page.close()

    def test_controlled_screenshot_failure_keeps_primary_error_and_private_values_out(self):
        check = _BudgetCheck(5.0)
        original_error = ValueError("SYNTHETIC_ORIGINAL_PRIVATE_VALUE")
        screenshot_error = "SYNTHETIC_SCREENSHOT_PRIVATE_VALUE"
        with tempfile.TemporaryDirectory(prefix="mjobs-t12-primary-error-") as directory:
            with patch.object(self.page, "evaluate", side_effect=AssertionError(
                    "diagnostic capture must not evaluate DOM")) as evaluate, \
                    patch.object(self.page, "screenshot", side_effect=RuntimeError(
                        screenshot_error)):
                result = self.capture(self.page, Path(directory) / "captures",
                                      "c" * 32, check, original_error)

            evaluate.assert_not_called()
            self.assertIsNotNone(result)
            metadata_path = result / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["error"]["type"], "ValueError")
            self.assertEqual(metadata["error"]["message_length"],
                             len("SYNTHETIC_ORIGINAL_PRIVATE_VALUE"))
            self.assertEqual(metadata["screenshot"], {"file": None, "status": "FAILED"})
            self.assert_dom_omitted(self, result)
            serialized = metadata_path.read_text(encoding="utf-8")
            serialized += (result / "dom.json").read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC_ORIGINAL_PRIVATE_VALUE", serialized)
            self.assertNotIn(screenshot_error, serialized)
            self.assertEqual(str(original_error), "SYNTHETIC_ORIGINAL_PRIVATE_VALUE")
            self.assertGreaterEqual(check.calls, 3)
            self.assertEqual(check.remaining_calls, 1)


if __name__ == "__main__":
    unittest.main()
