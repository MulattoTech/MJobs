"""Chromium denies cross-site API navigation while same-origin saves work."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest

from playwright.sync_api import sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web first")
class LocalOriginBrowserTests(unittest.TestCase):
    def test_browser_cross_site_navigation_cannot_read_saved_search(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-origin-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            sock = bound_browser_socket()
            port = sock.getsockname()[1]
            origin = f"http://localhost:{port}"
            target = f"http://127.0.0.1:{port}/search-profiles/browser-private"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root, operation_mode="PERSONAL"),
                log_level="error",
            ))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated origin API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(origin, wait_until="domcontentloaded")
                    saved = page.evaluate("""async () => {
                        const response = await fetch('/search-profiles', {
                            method: 'PUT', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({id: 'browser-private', profile_id: 'fixture',
                                name: 'Private browser search', enabled: false})
                        });
                        return response.status;
                    }""")
                    self.assertEqual(saved, 200)
                    page.reload(wait_until="domcontentloaded")
                    name = page.evaluate("""async () =>
                        (await (await fetch('/search-profiles/browser-private')).json()).name""")
                    self.assertEqual(name, "Private browser search")
                    page.evaluate("""url => {
                        const link = document.createElement('a');
                        link.href = url; link.textContent = 'Cross-site private search';
                        link.style.cssText = 'position:fixed;top:0;left:0;z-index:2147483647;background:white';
                        document.body.appendChild(link);
                    }""", target)
                    with page.expect_response(target) as denied:
                        page.get_by_role("link", name="Cross-site private search", exact=True).click()
                    self.assertEqual(denied.value.status, 403)
                    self.assertNotIn("Private browser search", denied.value.text())
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated origin API must stop")
