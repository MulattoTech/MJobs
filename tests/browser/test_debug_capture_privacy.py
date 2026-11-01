"""Synthetic browser-to-package privacy checks for failure diagnostics."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.applications import mock_adapter
from services.applications import debug_capture as debug_capture_module
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class DebugCapturePackagePrivacyTests(unittest.TestCase):
    def test_fixture_browser_failure_package_redacts_visual_and_serialized_canaries(self):
        canaries = (
            "SYNTHETIC_CONTACT@example.invalid",
            "SYNTHETIC_TOKEN_81f1a9",
            "SYNTHETIC_NAME_3a8f2d",
            "SYNTHETIC_FORM_VALUE_2c71b4",
            "SYNTHETIC_CANVAS_619fd0",
            "SYNTHETIC_IMAGE_04ad22",
            "SYNTHETIC_FAILURE_783ac4",
        )
        with tempfile.TemporaryDirectory() as directory, fixture_server() as origin:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = dict(PROFILE, id="fixture", name="SYNTHETIC_NAME_3a8f2d",
                           email="SYNTHETIC_FORM_VALUE_2c71b4@example.invalid")
            store.save_profile(profile)
            try:
                discover(store, origin, "fixture")
                job = next(row for row in store.jobs() if row["requisition"] == "B")
                app_id = prepare(store, job["id"], "fixture", root)
                from playwright.sync_api import Locator

                original_fill = Locator.fill
                original_capture = mock_adapter.capture_browser_failure
                baseline_path = root / "synthetic-unredacted-before.png"
                observed_boxes = {}

                def inject_canaries_and_fail(locator, value, *args, **kwargs):
                    result = original_fill(locator, value, *args, **kwargs)
                    if locator.get_attribute("name") == "email":
                        observed_boxes.update(locator.evaluate("""async el => {
                          const doc = el.ownerDocument;
                          doc.body.insertAdjacentHTML('beforeend', `
                            <section style="position:fixed;z-index:99999;top:12px;left:12px;
                                          background:white;color:black;font:24px Arial;padding:12px">
                              <p id="contact-canary">SYNTHETIC_CONTACT@example.invalid</p>
                              <p id="token-canary">SYNTHETIC_TOKEN_81f1a9</p>
                              <canvas id="canvas-canary" width="420" height="50"></canvas>
                              <img id="image-canary" width="240" height="44"
                                src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='44'%3E%3Ctext x='2' y='32' font-size='24'%3ESYNTHETIC_IMAGE_04ad22%3C/text%3E%3C/svg%3E">
                            </section>`);
                          const canvas = doc.querySelector('#canvas-canary');
                          const context = canvas.getContext('2d');
                          context.fillStyle = '#000';
                          context.font = '24px Arial';
                          context.fillText('SYNTHETIC_CANVAS_619fd0', 4, 34);
                          await doc.querySelector('#image-canary').decode();
                          const selectors = ['#contact-canary', '#token-canary', '#canvas-canary',
                                             '#image-canary', 'input[name=name]', 'input[name=email]'];
                          return Object.fromEntries(selectors.map(selector => {
                            const box = doc.querySelector(selector).getBoundingClientRect();
                            return [selector, {x: box.x, y: box.y, width: box.width, height: box.height}];
                          }));
                        }"""))
                        raise RuntimeError("SYNTHETIC_FAILURE_783ac4 SYNTHETIC_EXCEPTION_TOKEN_3f65a2")
                    return result

                def capture_with_fixture_baseline(page, *args, **kwargs):
                    page.screenshot(path=str(baseline_path), full_page=False)
                    return original_capture(page, *args, **kwargs)

                with patch.object(Locator, "fill", inject_canaries_and_fail), \
                        patch.object(mock_adapter, "capture_browser_failure", capture_with_fixture_baseline):
                    with self.assertRaisesRegex(RuntimeError, "SYNTHETIC_FAILURE_783ac4"):
                        apply_mock(store, app_id, origin, root, debug_capture=True)

                self.assertEqual(store.application(app_id)["state"], "REVIEW_REQUIRED")
                self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(),
                                 {"status": "not_found"})
                captures = list((root / app_id / "debug-captures").glob("*/metadata.json"))
                self.assertEqual(len(captures), 1)
                metadata_path = captures[0]
                capture = metadata_path.parent
                screenshot_path = capture / "screenshot.png"
                dom_path = capture / "dom.json"
                self.assertEqual({path.name for path in capture.iterdir()},
                                 {"screenshot.png", "metadata.json", "dom.json"})

                serialized = metadata_path.read_text(encoding="utf-8")
                serialized += dom_path.read_text(encoding="utf-8")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                dom = json.loads(dom_path.read_text(encoding="utf-8"))
                self.assertEqual(dom["status"], "CAPTURED")
                self.assertGreater(dom["element_counts"].get("form", 0), 0)
                self.assertGreater(dom["element_counts"].get("input", 0), 0)
                for canary in canaries + ("SYNTHETIC_EXCEPTION_TOKEN_3f65a2",):
                    self.assertNotIn(canary, serialized)
                self.assertNotIn("<html", serialized.lower())
                self.assertNotIn("message", metadata["error"])
                self.assertEqual(metadata["current_step"], "FILL_CONTACT")
                self.assertEqual(metadata["current_url"], origin + "/:redacted/:redacted/:redacted")
                self.assertEqual(metadata["retention_days"], 30)
                self.assertEqual(metadata["max_captures_per_application"], 20)

                with Image.open(screenshot_path) as image:
                    screenshot = image.convert("RGB")
                    with Image.open(baseline_path) as image:
                        baseline = image.convert("RGB")
                    for selector in ("#contact-canary", "#token-canary", "#canvas-canary", "#image-canary"):
                        box = observed_boxes[selector]
                        crop = screenshot.crop((int(box["x"]), int(box["y"]),
                                                int(box["x"] + box["width"]),
                                                int(box["y"] + box["height"])))
                        baseline_crop = baseline.crop((int(box["x"]), int(box["y"]),
                                                       int(box["x"] + box["width"]),
                                                       int(box["y"] + box["height"])))
                        self.assertTrue(self._contains_visible_content(baseline_crop), selector)
                        self.assertTrue(self._uniformly_redacted_or_blank(crop), selector)
                    for selector in ("input[name=name]", "input[name=email]"):
                        field = observed_boxes[selector]
                        field_box = (int(field["x"]), int(field["y"]),
                                     int(field["x"] + field["width"]),
                                     int(field["y"] + field["height"]))
                        baseline_value_region = baseline.crop(field_box)
                        captured_value_region = screenshot.crop(field_box)
                        self.assertTrue(self._contains_visible_content(baseline_value_region), selector)
                        self.assertTrue(self._fully_masked_black(captured_value_region), selector)

                        if selector == "input[name=email]":
                            one_pixel_leak = captured_value_region.copy()
                            one_pixel_leak.putpixel(
                                (one_pixel_leak.width - 1, one_pixel_leak.height // 2),
                                (255, 255, 255),
                            )
                            self.assertFalse(self._fully_masked_black(one_pixel_leak))
            finally:
                store.engine.dispose()

    def test_native_dom_snapshot_has_bounded_topology_without_private_attributes(self):
        marker = "SYNTHETIC_PRIVATE_DOM_72b1"
        html = f'''<main role="main"><form id="{marker}" action="/{marker}?token={marker}">
          <input name="{marker}" value="{marker}" type="password" role="textbox">
          <textarea>{marker}</textarea><x-private-dom-72b1 role="{marker}">{marker}</x-private-dom-72b1>
          </form>{'<section role="progressbar"></section>' * 1000}</main>'''
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(html)
                capture = debug_capture_module.capture_browser_failure(
                    page, Path(directory), attempt_id="2" * 32, step="VALIDATE",
                    configured_url="https://fixture.invalid/apply", error=RuntimeError(marker))
                self.assertIsNotNone(capture)
                raw = (capture / "dom.json").read_bytes()
                dom = json.loads(raw)
                self.assertEqual(dom["status"], "CAPTURED")
                self.assertTrue(dom["truncated"])
                self.assertLessEqual(len(raw), debug_capture_module.MAX_DOM_BYTES)
                self.assertLessEqual(len(dom["nodes"]), debug_capture_module.MAX_DOM_NODES)
                self.assertEqual(dom["role_counts"].get("textbox"), 1)
                self.assertNotIn(marker, raw.decode())
                self.assertNotIn("x-private-dom-72b1", raw.decode())
                self.assertNotIn("password", raw.decode())
                self.assertEqual(sum(dom["element_counts"].values()), len(dom["nodes"]))
                for index, node in enumerate(dom["nodes"]):
                    self.assertTrue(node["parent"] is None or 0 <= node["parent"] < index)
            finally:
                browser.close()

    def test_unacknowledged_native_dom_abort_is_bounded_and_transport_recovers(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content("<main><form><input role='textbox'></form></main>")
                connection = page._impl_obj._connection
                transport = connection._transport
                original_send = transport.send
                blocked = []
                aborts = []
                guards = []

                def release_callback(identity):
                    callback = connection._callbacks.get(identity)
                    if callback is not None and not callback.future.done():
                        callback.future.cancel()

                def withhold_owned_dom_reply(message):
                    params = message.get("params", {})
                    if message.get("method") == "send" and params.get("method") == "DOM.getDocument":
                        blocked.append(message["id"])
                        # Fail promptly under the old unbounded abort behavior.
                        guards.append(connection._loop.call_later(1.5, release_callback, message["id"]))
                        return
                    if message.get("method") == "__abort__" and params.get("id") in blocked:
                        aborts.append(params["id"])
                        return
                    original_send(message)

                try:
                    with patch.object(transport, "send", side_effect=withhold_owned_dom_reply):
                        started = time.monotonic()
                        dom = debug_capture_module._dom_snapshot(
                            page, deadline=started + .6, checkpoint=lambda: None)
                        elapsed = time.monotonic() - started
                finally:
                    for guard in guards:
                        guard.cancel()
                self.assertEqual(dom["status"], "OMITTED_TIMEOUT")
                self.assertLess(elapsed, 1)
                self.assertEqual(len(blocked), 1)
                self.assertEqual(aborts, blocked)
                self.assertTrue(connection._callbacks[blocked[0]].future.cancelled())
                self.assertFalse(transport.on_error_future.done())

                async def remaining_owned_tasks():
                    return sum(task.get_name() == "mjobs-debug-dom-operation" for task in asyncio.all_tasks())

                self.assertEqual(page._sync(remaining_owned_tasks()), 0)
                recovered = debug_capture_module._dom_snapshot(
                    page, deadline=time.monotonic() + .6, checkpoint=lambda: None)
                self.assertEqual(recovered["status"], "CAPTURED")
            finally:
                browser.close()

    @staticmethod
    def _uniformly_redacted_or_blank(image):
        pixels = set(image.get_flattened_data())
        return pixels.issubset({(0, 0, 0), (255, 255, 255)})

    @staticmethod
    def _contains_visible_content(image):
        return any(0 < max(pixel) < 255 for pixel in image.get_flattened_data())

    @staticmethod
    def _fully_masked_black(image):
        return image.width > 0 and image.height > 0 and all(
            pixel == (0, 0, 0) for pixel in image.get_flattened_data()
        )


if __name__ == "__main__":
    unittest.main()
