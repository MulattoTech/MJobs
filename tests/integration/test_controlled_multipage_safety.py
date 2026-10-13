"""Negative browser-boundary checks for the controlled two-page fixture."""
import hashlib
import json
import unittest
from unittest.mock import patch

from tests.integration import test_controlled_application_dry_run as contract
from services.applications import controlled_dry_run as service


UNAUTHORIZED_LOCAL = "http://127.0.0.1:1/mjobs-controlled-application/unauthorized"
EXTERNAL = "https://example.invalid/collect"


class ControlledMultipageSafetyTests(unittest.TestCase):
    _new_store = contract.ControlledApplicationDryRunTests._new_store
    setUp = contract.ControlledApplicationDryRunTests.setUp
    tearDown = contract.ControlledApplicationDryRunTests.tearDown
    path = contract.ControlledApplicationDryRunTests.path
    execute = contract.ControlledApplicationDryRunTests.execute
    review_and_queue = contract.ControlledApplicationDryRunTests.review_and_queue

    def _fixture_run(self, mutate, request_id="unsafe-multipage"):
        original = service.template

        def altered(provider):
            fixture = original(provider)
            mutate(fixture)
            fixture["template_sha256"] = hashlib.sha256(json.dumps(
                fixture, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return fixture

        with patch.object(service, "template", altered), \
             patch.object(service, "run_browser", wraps=service.run_browser) as browser_spy:
            _, body, queued = self.review_and_queue(request_id)
            result = self.execute(queued["operation_id"])
            saved = self.client.get(self.path()+"/runs/"+body["request_id"]).json()
        browser_spy.assert_called_once()
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(saved["error_code"], "CONTROLLED_DRY_RUN_REVIEW_REQUIRED", saved)
        self.assertIsNone(saved["receipt"], saved)
        self.assertEqual(self.store.count("application_attempts"), 0)
        return result, saved

    def test_external_and_unlisted_next_links_fail_before_navigation(self):
        for index, target in enumerate((EXTERNAL, "http://127.0.0.1:1/mjobs-controlled-application/unlisted")):
            with self.subTest(target=target):
                self._fixture_run(lambda fixture: fixture.__setitem__(
                    "html", fixture["html"].replace(
                        service.ORIGIN+"/documents", target)), f"unsafe-link-{index}")

    def test_extra_frame_or_submit_button_fails_closed(self):
        def add_frame(fixture):
            fixture["html"] = fixture["html"].replace(
                "</form>", '<iframe src="'+UNAUTHORIZED_LOCAL+'"></iframe></form>', 1)

        def add_button(fixture):
            fixture["html"] = fixture["html"].replace(
                "</form>", '<button type="submit">Continue</button></form>', 1)

        for name, mutate in (("frame", add_frame), ("button", add_button)):
            with self.subTest(control=name):
                self._fixture_run(mutate, f"unsafe-control-{name}")

    def test_unexpected_local_navigation_attempt_is_intercepted(self):
        from playwright.sync_api import Locator

        original_click = Locator.click
        navigation_attempts = []
        intercepted_attempts = []

        def navigate_to_unlisted(locator, *args, **kwargs):
            if locator.get_attribute("data-controlled-next") is not None:
                # The fixed loopback URL is intercepted by the context route; no
                # employer or external host is contacted by this adversarial test.
                navigation_attempts.append(UNAUTHORIZED_LOCAL)
                try:
                    return locator.page.goto(UNAUTHORIZED_LOCAL, wait_until="domcontentloaded")
                except Exception:
                    intercepted_attempts.append(True)
                    raise
            return original_click(locator, *args, **kwargs)

        with patch.object(service, "run_browser", wraps=service.run_browser) as browser_spy, \
             patch.object(Locator, "click", navigate_to_unlisted):
            _, body, queued = self.review_and_queue("unexpected-navigation")
            result = self.execute(queued["operation_id"])
            saved = self.client.get(self.path()+"/runs/"+body["request_id"]).json()

        browser_spy.assert_called_once()
        self.assertEqual(navigation_attempts, [UNAUTHORIZED_LOCAL])
        self.assertEqual(intercepted_attempts, [True])
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(saved["error_code"], "CONTROLLED_DRY_RUN_BROWSER_FAILED", saved)
        self.assertIsNone(saved["receipt"], saved)
        self.assertEqual(self.store.count("application_attempts"), 0)


if __name__ == "__main__":
    unittest.main()
