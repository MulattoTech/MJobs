"""Rendered links from synthetic Remotive collection, without provider navigation."""
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web first")
class RemotiveLinkSafetyBrowserTests(unittest.TestCase):
    def test_collected_unsafe_link_absent_and_valid_link_preserved_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-remotive-link-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'test.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            valid = RAW_REMOTIVE_PAYLOAD["jobs"][0]["url"]
            unsafe = "https://remotive.com.attacker.invalid/jobs/1"
            payload = {"jobs": [dict(RAW_REMOTIVE_PAYLOAD["jobs"][0], id=8001,
                                     title="Unsafe Link Engineer", url=unsafe),
                                dict(RAW_REMOTIVE_PAYLOAD["jobs"][1], id=8002,
                                     title="Valid Link Engineer", url=valid)]}

            def transport(request):
                self.assertEqual(str(request.url), "https://remotive.com/api/remote-jobs")
                return httpx.Response(200, json=payload)

            source_id = configure_source(store, "default", provider="remotive")
            collect_source(store, source_id, "fixture", transport=httpx.MockTransport(transport))
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external, submissions, errors = [], [], []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Controlled API startup failed")
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        elif parsed.path == "/applications" and route.request.method == "POST":
                            submissions.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    for _ in range(2):
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.get_by_label("Active profile", exact=True).select_option(str(PROFILE["id"]))
                        for title, expected_url in (("Unsafe Link Engineer", ""), ("Valid Link Engineer", valid)):
                            page.get_by_role("button", name=re.compile(r"^Jobs(?:\s|$)")).click()
                            page.get_by_role("button", name=re.compile(title)).click()
                            expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                            expect(page.locator(f'a[href="{unsafe}"]')).to_have_count(0)
                            link = page.get_by_role("link", name="Open employer application ↗", exact=True)
                            saved = page.get_by_role("link", name="Open saved application link ↗", exact=True)
                            if expected_url:
                                expect(link).to_have_attribute("href", expected_url)
                                expect(saved).to_have_attribute("href", expected_url)
                            else:
                                expect(link).to_have_count(0)
                                expect(saved).to_have_count(0)
                        page.reload(wait_until="domcontentloaded")
                        expect(page.locator(f'a[href="{unsafe}"]')).to_have_count(0)
                        page.close()
                    self.assertEqual(external, [])
                    self.assertEqual(submissions, [])
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
