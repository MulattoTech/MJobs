"""Chromium referrer fallback with explicitly stripped proxy metadata."""
import copy
from html import escape
from pathlib import Path
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web first")
class ReferrerSecurityBrowserTests(unittest.TestCase):
    def test_real_referrer_denies_foreign_private_navigation_without_fetch_metadata(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-referrer-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            with TestClient(app) as client:
                saved = client.put("/search-profiles", json={
                    "id": "referrer-private", "profile_id": "fixture",
                    "name": "Synthetic private search", "enabled": False,
                })
                self.assertEqual(saved.status_code, 200, saved.text)
            received = []

            async def observe(scope, receive, send):
                if scope["type"] == "http" and scope["path"] == "/search-profiles/referrer-private":
                    incoming = {key.decode(): value.decode() for key, value in scope["headers"]}
                    # Emulate a legacy/proxy path only at the test boundary.
                    # Chromium itself sends fetch metadata and can re-add it
                    # after request interception, so assert the forwarded scope.
                    scope = {**scope, "headers": [(key, value) for key, value in scope["headers"]
                             if key not in {b"origin", b"sec-fetch-site",
                                            b"sec-fetch-mode", b"sec-fetch-dest"}]}
                    headers = {key.decode(): value.decode() for key, value in scope["headers"]}
                    received.append({
                        "incoming_fetch_site": incoming.get("sec-fetch-site"),
                        "forwarded": {name: headers.get(name) for name in
                                      ("origin", "referer", "sec-fetch-site")},
                    })
                await app(scope, receive, send)

            sock = bound_browser_socket()
            port = sock.getsockname()[1]
            origin = f"http://localhost:{port}"
            target = origin + "/search-profiles/referrer-private"
            foreign = f"http://127.0.0.1:{port}/legacy-browser-fixture"
            server = uvicorn.Server(uvicorn.Config(observe, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated referrer API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.set_default_timeout(10000)

                        page.goto(origin, wait_until="domcontentloaded")
                        allowed = page.evaluate("""async () => {
                            const response = await fetch('/search-profiles/referrer-private',
                                {referrerPolicy: 'same-origin'});
                            return {status: response.status, value: await response.json()};
                        }""")
                        self.assertEqual(allowed["status"], 200)
                        self.assertEqual(allowed["value"]["name"], "Synthetic private search")
                        self.assertEqual(received[-1]["incoming_fetch_site"], "same-origin")
                        self.assertEqual(received[-1]["forwarded"], {
                            "origin": None, "referer": origin + "/", "sec-fetch-site": None,
                        })

                        page.route(foreign, lambda route: route.fulfill(
                            status=200, content_type="text/html",
                            headers={"Referrer-Policy": "origin"},
                            body=f'<a href="{escape(target, quote=True)}">Foreign private link</a>',
                        ))
                        page.goto(foreign, wait_until="domcontentloaded")
                        with page.expect_response(target) as denied:
                            page.get_by_role("link", name="Foreign private link", exact=True).click()
                        self.assertEqual(denied.value.status, 403)
                        self.assertNotIn("Synthetic private search", denied.value.text())
                        self.assertEqual(received[-1]["incoming_fetch_site"], "cross-site")
                        self.assertEqual(received[-1]["forwarded"], {
                            "origin": None, "referer": f"http://127.0.0.1:{port}/",
                            "sec-fetch-site": None,
                        })
                        self.assertEqual(denied.value.headers.get("cache-control"), "no-store")
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated referrer API must stop")
