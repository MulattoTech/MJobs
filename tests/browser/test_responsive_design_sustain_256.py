"""Bounded MJ-REQ-255 Jobs layout and keyboard oracle; synthetic local UI only."""
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


ROOT = Path(os.environ.get("MJOBS_BROWSER_APP_ROOT", Path(__file__).resolve().parents[2]))


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ResponsiveJobsBrowserTests(unittest.TestCase):
    def test_jobs_fit_desktop_tablet_and_mobile_with_keyboard_selection(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-responsive-256-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "responsive-company", "name": "Fixture Company", "domain": "",
                })
                for index, title in enumerate(("Alpha Engineer", "Beta Engineer")):
                    job_id = f"responsive-job-{index}"
                    upsert(connection, m.jobs, {
                        "id": job_id, "company_id": "responsive-company",
                        "requisition": str(index), "title": title,
                        "description": "Synthetic local layout fixture.", "skills": [],
                        "location": "Remote", "remote_type": "REMOTE",
                    })
                    upsert(connection, m.routes, {
                        "id": f"{job_id}-route", "job_id": job_id,
                        "company_id": "responsive-company", "provider": "mock-ats",
                        "tenant": "fixture", "requisition": str(index),
                        "url": f"https://jobs.example.test/{job_id}",
                    })

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            blocked = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Synthetic responsive API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context()

                        def local_reads_only(route):
                            request = route.request
                            if (urlsplit(request.url).netloc != urlsplit(origin).netloc
                                    or request.method not in {"GET", "HEAD"}):
                                blocked.append((request.method, request.url))
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_reads_only)
                        context.add_init_script(
                            "localStorage.setItem('mjobs-profile', 'fixture')")
                        page = context.new_page()
                        page.set_default_timeout(8000)
                        for width, height, columns in (
                                (1440, 1000, 2), (861, 1112, 2), (860, 1112, 1),
                                (834, 1112, 1), (390, 844, 1)):
                            with self.subTest(width=width, height=height):
                                page.set_viewport_size({"width": width, "height": height})
                                page.goto(origin, wait_until="domcontentloaded")
                                cards = page.locator(".job-list .job-card")
                                expect(cards).to_have_count(2)
                                for title in ("Alpha Engineer", "Beta Engineer"):
                                    expect(page.get_by_role(
                                        "button", name=f"Fixture Company {title}",
                                        exact=True)).to_be_visible()

                                # DOM order, not fixture sort order, defines Tab traversal.
                                first, second = cards.nth(0), cards.nth(1)
                                first.focus()
                                page.keyboard.press("Tab")
                                expect(second).to_be_focused()
                                page.keyboard.press("Enter")
                                expect(second).to_have_class(re.compile(r"\bselected\b"))
                                detail = page.locator(".job-detail")
                                expect(detail.locator(":scope > h2")).to_have_text(
                                    second.locator(".job-card-title").inner_text())
                                page.keyboard.press("Shift+Tab")
                                expect(first).to_be_focused()
                                page.keyboard.press("Space")
                                expect(first).to_have_class(re.compile(r"\bselected\b"))
                                expect(detail.locator(":scope > h2")).to_have_text(
                                    first.locator(".job-card-title").inner_text())

                                expect(page.locator(".job-list")).to_be_visible()
                                expect(detail).to_be_visible()
                                actual_columns = page.locator(".job-grid").evaluate(
                                    "el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
                                dimensions = page.evaluate("""() => ({
                                    content: document.documentElement.scrollWidth,
                                    viewport: innerWidth
                                })""")
                                if dimensions["content"] > dimensions["viewport"]:
                                    dimensions["overflowing"] = page.evaluate("""() =>
                                        [...document.querySelectorAll('body *')].map(el => {
                                            const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                                            return {tag:el.tagName,cls:el.className,left:r.left,right:r.right,
                                                width:r.width,minWidth:s.minWidth,grid:s.gridTemplateColumns,
                                                whiteSpace:s.whiteSpace,overflow:s.overflow};
                                        }).filter(x => x.right > innerWidth + 1).slice(0,18)
                                    """)
                                self.assertEqual(
                                    (actual_columns, dimensions["content"] <= dimensions["viewport"]),
                                    (columns, True),
                                    f"columns={actual_columns}, dimensions={dimensions}")
                        self.assertEqual(blocked, [], "UI must remain local and read-only")
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
