"""Connected browser acceptance for durable, profile-isolated AI task settings."""
import copy
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
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class AITaskSettingsBrowserTests(unittest.TestCase):
    def test_settings_save_reload_and_remain_isolated_between_profiles(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-ai-task-settings-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'settings.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            first = copy.deepcopy(PROFILE)
            second = copy.deepcopy(PROFILE)
            second.update(id="other-profile", name="Other Profile", email="other@example.test")
            store.save_profile(first)
            store.save_profile(second)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="PERSONAL"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated task-settings API did not start")
                    time.sleep(0.02)

                external, errors = [], []
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
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
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="AI task settings")
                    expect(panel).to_be_visible()
                    self.assertEqual(panel.locator(".ai-task-settings fieldset").count(), 7)
                    provider = panel.get_by_label("Job scoring provider", exact=True)
                    model = panel.get_by_label("Job scoring model", exact=True)
                    expect(provider).to_have_value("disabled")
                    expect(model).to_be_disabled()
                    provider.select_option("ollama")
                    model.fill("llama3.2:3b")
                    panel.get_by_label("Resume tailoring provider", exact=True).select_option("ollama")
                    panel.get_by_label("Resume tailoring model", exact=True).fill("qwen2.5:3b")
                    panel.get_by_role("button", name="Save AI task settings", exact=True).click()
                    expect(page.get_by_role("status").filter(
                        has_text="AI task settings saved for this profile.")).to_be_visible()

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="AI task settings")
                    expect(panel.get_by_label("Job scoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Job scoring model", exact=True)).to_have_value("llama3.2:3b")
                    expect(panel.get_by_label("Resume tailoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Resume tailoring model", exact=True)).to_have_value("qwen2.5:3b")

                    page.locator("#active-profile").select_option("other-profile")
                    expect(panel.get_by_label("Job scoring provider", exact=True)).to_have_value("disabled")
                    expect(panel.get_by_label("Job scoring model", exact=True)).to_be_disabled()
                    expect(panel.get_by_label("Resume tailoring provider", exact=True)).to_have_value("disabled")
                    expect(panel.get_by_label("Resume tailoring model", exact=True)).to_be_disabled()
                    page.locator("#active-profile").select_option("fixture")
                    expect(panel.get_by_label("Job scoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Job scoring model", exact=True)).to_have_value("llama3.2:3b")
                    expect(panel.get_by_label("Resume tailoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Resume tailoring model", exact=True)).to_have_value("qwen2.5:3b")

                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(panel).to_be_visible()
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated task-settings API must stop")


if __name__ == "__main__":
    unittest.main()
