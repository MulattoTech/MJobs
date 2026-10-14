"""Real sibling-port navigation cannot receive saved private search records."""
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class LocalOriginPrivacyBrowserTests(unittest.TestCase):
    def test_saved_search_survives_sibling_origin_denial_and_shell_navigation(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-origin-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            sockets = [bound_browser_socket(), bound_browser_socket()]
            origin, sibling = [f"http://127.0.0.1:{sock.getsockname()[1]}" for sock in sockets]
            saved = {}
            attack = FastAPI()

            @attack.get("/", response_class=HTMLResponse)
            def sibling_page():
                return (f'<a href="{origin}/search-profiles/{saved["id"]}">Private record</a>'
                        f'<a href="{origin}/">Open MJobs</a>')

            apps = [create_app(store, "http://127.0.0.1:1", root,
                               "redis://127.0.0.1:1/0", operation_mode="FIXTURE"), attack]
            received = []
            api = apps[0]

            async def observe_api(scope, receive, send):
                if scope["type"] == "http" and scope["path"].startswith("/search-profiles/"):
                    received.append(dict(scope["headers"]))
                await api(scope, receive, send)

            apps[0] = observe_api
            servers = [uvicorn.Server(uvicorn.Config(app, log_level="error")) for app in apps]
            threads = [threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                       for server, sock in zip(servers, sockets)]
            for thread in threads:
                thread.start()
            try:
                deadline = time.monotonic() + 10
                while not all(server.started for server in servers):
                    if not all(thread.is_alive() for thread in threads) or time.monotonic() > deadline:
                        self.fail("Isolated origin test servers did not start")
                    time.sleep(.02)

                external = []
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()

                    def block_external(route):
                        external.append(route.request.url)
                        route.abort()

                    # Leave local requests unintercepted: route.continue_ can
                    # change browser-generated Fetch Metadata on navigation.
                    local_hosts = "|".join(re.escape(urlsplit(url).netloc) for url in (origin, sibling))
                    context.route(re.compile(r"^https?://(?!(?:" + local_hosts + r")(?:/|$))"),
                                  block_external)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    page.get_by_role("button", name="Use remote IT infrastructure example", exact=True).click()
                    name = "Private synthetic sibling-origin search"
                    page.get_by_label("Search name", exact=True).fill(name)
                    page.get_by_role("button", name="Save search", exact=True).click()
                    expect(page.get_by_role("region", name=f"Schedule “{name}”")).to_be_visible()
                    response = page.request.get(origin + "/search-profiles?profile_id=fixture")
                    self.assertEqual(response.status, 200)
                    saved.update(next(row for row in response.json()["search_profiles"] if row["name"] == name))

                    page.goto(sibling, wait_until="domcontentloaded")
                    with page.expect_response(origin + "/search-profiles/" + saved["id"]) as observed:
                        page.get_by_role("link", name="Private record", exact=True).click()
                    rejected = observed.value
                    self.assertEqual(received[-1].get(b"sec-fetch-site"), b"same-site", received[-1])
                    self.assertEqual(rejected.status, 403)
                    self.assertNotIn(name, rejected.text())

                    page.goto(sibling, wait_until="domcontentloaded")
                    page.get_by_role("link", name="Open MJobs", exact=True).click()
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_role("region", name=f"Schedule “{name}”")).to_be_visible()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_role("region", name=f"Schedule “{name}”")).to_be_visible()
                    browser.close()
                self.assertEqual(external, [])
            finally:
                for server in servers:
                    server.should_exit = True
                for thread, sock in zip(threads, sockets):
                    thread.join(timeout=10)
                    sock.close()
                    self.assertFalse(thread.is_alive(), "Owned origin test server must stop")
                store.engine.dispose()
            reopened = Store(database_url)
            try:
                from services.discovery.search_profiles import get_search_profile
                self.assertEqual(get_search_profile(reopened, saved["id"])["name"], name)
            finally:
                reopened.engine.dispose()
