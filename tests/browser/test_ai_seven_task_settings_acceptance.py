"""MJ-REQ-109 connected acceptance: configure seven choices without inference."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration import test_ai_seven_task_settings_acceptance as contract


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SevenTaskSettingsBrowserAcceptanceTests(unittest.TestCase):
    def _assert_fields(self, panel, selections):
        expect(panel.locator(".ai-task-settings fieldset")).to_have_count(7)
        for task, label in contract.TASK_LABELS:
            selection = selections[task]
            provider = panel.get_by_label(f"{label} provider", exact=True)
            model = panel.get_by_label(f"{label} model", exact=True)
            expect(provider).to_be_enabled()
            expect(provider).to_have_value(selection["provider"])
            expect(model).to_have_value(selection["model"] or "")
            if selection["provider"] == "disabled":
                expect(model).to_be_disabled()
            else:
                expect(model).to_be_enabled()

    def _fill_fields(self, panel, selections):
        for task, label in contract.TASK_LABELS:
            panel.get_by_label(f"{label} provider", exact=True).select_option("ollama")
            panel.get_by_label(f"{label} model", exact=True).fill(selections[task]["model"])

    def _save(self, page, panel, profile_id, selections, store):
        path = f"/profiles/{profile_id}/ai-task-settings"
        with page.expect_response(lambda response:
                urlsplit(response.url).path == path and response.request.method == "PUT") as saved:
            panel.get_by_role("button", name="Save AI task settings", exact=True).click()
        response = saved.value
        self.assertEqual(response.status, 200, response.text())
        self.assertEqual(response.json()["profile_id"], profile_id)
        self.assertEqual(response.json()["tasks"], selections)
        expect(page.get_by_role("status").filter(
            has_text="AI task settings saved for this profile.")).to_be_visible()
        self.assertEqual(store.ai_task_settings(profile_id), selections)

    def test_all_seven_choices_save_reload_and_stay_isolated_between_profiles(self):
        first = contract.model_selections("alpha")
        second = contract.model_selections("beta")
        with tempfile.TemporaryDirectory(prefix="mjobs-seven-task-browser-") as directory, \
                contract.forbid_provider_requests() as guards:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'settings.db').as_posix()}"
            store = Store(database_url)
            self.addCleanup(store.engine.dispose)
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            contract.seed_profiles(store)
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="PERSONAL"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Seven-task settings API did not start")
                    time.sleep(0.02)

                external, errors = [], []
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(viewport={"width": 1280, "height": 900})

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.locator("#active-profile").select_option(contract.FIRST_PROFILE)
                        page.get_by_role("button", name="AI Settings", exact=True).click()
                        panel = page.get_by_role("region", name="AI task settings")
                        self._assert_fields(panel, contract.disabled_selections())
                        self._fill_fields(panel, first)
                        self._save(page, panel, contract.FIRST_PROFILE, first, store)

                        page.reload(wait_until="domcontentloaded")
                        expect(page.locator("#active-profile")).to_have_value(contract.FIRST_PROFILE)
                        page.get_by_role("button", name="AI Settings", exact=True).click()
                        panel = page.get_by_role("region", name="AI task settings")
                        self._assert_fields(panel, first)

                        page.locator("#active-profile").select_option(contract.SECOND_PROFILE)
                        self._assert_fields(panel, contract.disabled_selections())
                        self._fill_fields(panel, second)
                        self._save(page, panel, contract.SECOND_PROFILE, second, store)
                        self.assertEqual(store.ai_task_settings(contract.FIRST_PROFILE), first)

                        page.reload(wait_until="domcontentloaded")
                        expect(page.locator("#active-profile")).to_have_value(contract.SECOND_PROFILE)
                        page.get_by_role("button", name="AI Settings", exact=True).click()
                        panel = page.get_by_role("region", name="AI task settings")
                        self._assert_fields(panel, second)
                        page.locator("#active-profile").select_option(contract.FIRST_PROFILE)
                        self._assert_fields(panel, first)

                        # Every individual editor must preserve its six neighbors.
                        updates = contract.model_selections("alpha", revision=2)
                        for task, label in contract.TASK_LABELS:
                            panel.get_by_label(f"{label} model", exact=True).fill(updates[task]["model"])
                            first[task] = updates[task]
                            self._assert_fields(panel, first)
                        self._save(page, panel, contract.FIRST_PROFILE, first, store)
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="AI Settings", exact=True).click()
                        panel = page.get_by_role("region", name="AI task settings")
                        self._assert_fields(panel, first)
                        page.locator("#active-profile").select_option(contract.SECOND_PROFILE)
                        self._assert_fields(panel, second)
                        self.assertEqual(errors, [])
                        self.assertEqual(external, [])
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                self.assertFalse(thread.is_alive(), "Seven-task settings API must stop")

            # Read with another engine after the browser and server have stopped.
            reopened = Store(database_url)
            try:
                self.assertEqual(reopened.ai_task_settings(contract.FIRST_PROFILE), first)
                self.assertEqual(reopened.ai_task_settings(contract.SECOND_PROFILE), second)
            finally:
                reopened.engine.dispose()
                store.engine.dispose()
            for guard in guards:
                guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
