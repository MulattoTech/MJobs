"""Synthetic Chromium capture persistence, interruption and isolation checks."""
import json
import time

from PIL import Image
import pytest
from playwright.sync_api import TimeoutError as BrowserTimeoutError, sync_playwright

from services.applications.browser_capture import capture_redacted_screenshot
from services.applications.debug_capture import capture_browser_failure


def test_capture_package_readback_survives_closed_page_and_next_operation(tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=5000)
        try:
            page = browser.new_page(viewport={"width": 600, "height": 220})
            page.set_default_timeout(1000)
            page.set_content("""
                <style>body { background: white; font: 20px Arial }</style>
                <form><input value="SYNTHETIC_PRIVATE_ANSWER"></form>
                <p>synthetic@example.test</p>
            """, timeout=1000)
            first = capture_browser_failure(
                page, tmp_path, attempt_id="a" * 32, step="DETECT_FORM",
                configured_url="https://fixture.invalid/private?token=synthetic",
                error=RuntimeError("SYNTHETIC_PRIVATE_ANSWER"),
            )
            metadata_bytes = (first / "metadata.json").read_bytes()
            metadata = json.loads(metadata_bytes)
            assert metadata["screenshot"]["status"] == "CAPTURED"
            assert b"SYNTHETIC_PRIVATE_ANSWER" not in metadata_bytes
            with Image.open(first / "screenshot.png") as image:
                assert image.format == "PNG"
                assert image.width == 600
            assert page.locator("input").input_value() == "SYNTHETIC_PRIVATE_ANSWER"
            assert page.locator("[data-mjobs-sensitive-mask]").count() == 0

            page.close()
            interrupted = capture_browser_failure(
                page, tmp_path, attempt_id="b" * 32, step="DETECT_FORM",
                configured_url="https://fixture.invalid/",
                error=RuntimeError("synthetic interruption"),
            )
            interrupted_metadata = json.loads((interrupted / "metadata.json").read_bytes())
            assert interrupted_metadata["screenshot"]["status"] == "FAILED"
            assert not (interrupted / "screenshot.png").exists()

            next_page = browser.new_page()
            next_page.set_content("<input value='synthetic next operation'>", timeout=1000)
            capture_redacted_screenshot(next_page, tmp_path / "next.png", timeout_ms=1000)
            with Image.open(tmp_path / "next.png") as image:
                assert image.format == "PNG"
            assert (first / "metadata.json").read_bytes() == metadata_bytes
        finally:
            browser.close()


def test_busy_renderer_capture_times_out_and_independent_page_still_works(tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=5000)
        try:
            page = browser.new_page()
            page.set_content("<p>synthetic fixture</p>", timeout=1000)
            # Finite synthetic renderer stall: no infinite loop or external URL.
            page.evaluate("""() => setTimeout(() => {
                const end = performance.now() + 1000;
                while (performance.now() < end) {}
            }, 0)""")
            started = time.monotonic()
            with pytest.raises(BrowserTimeoutError):
                capture_redacted_screenshot(page, tmp_path / "busy.png", timeout_ms=100)
            assert time.monotonic() - started < 2

            next_page = browser.new_page()
            next_page.set_content("<p>synthetic independent fixture</p>", timeout=1000)
            capture_redacted_screenshot(next_page, tmp_path / "healthy.png", timeout_ms=2000)
            with Image.open(tmp_path / "healthy.png") as image:
                assert image.format == "PNG"
        finally:
            browser.close()
