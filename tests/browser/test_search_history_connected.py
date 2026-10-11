"""Connected FIXTURE browser acceptance for durable search-run history."""
from datetime import datetime
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SearchHistoryConnectedBrowserTests(unittest.TestCase):
    def test_completed_fixture_search_history_is_durable_and_profile_scoped(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-search-history-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'search-history.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})
            save_search_profile(store, {
                "id": "fixture-search",
                "profile_id": "fixture",
                "name": "Synthetic systems administrator search",
                "enabled": False,
                "include_titles": ["Systems Administrator"],
                "remote": "REQUIRED",
                "employment_types": [],
                "source_ids": [],
            })

            with fixture_server() as source_origin:
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                app = create_app(store, source_origin, root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                server_thread = threading.Thread(
                    target=server.run, kwargs={"sockets": [sock]}, daemon=True,
                )
                external_requests = []
                submission_requests = []
                page_errors = []
                try:
                    server_thread.start()
                    # Local startup readiness; no external services are contacted.
                    from time import monotonic, sleep
                    deadline = monotonic() + 10
                    while not server.started:
                        if not server_thread.is_alive() or monotonic() > deadline:
                            self.fail("Isolated search-history API did not start")
                        sleep(0.02)

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context()

                        def local_fixture_only(route):
                            request = route.request
                            parsed = urlsplit(request.url)
                            if parsed.netloc != urlsplit(origin).netloc:
                                external_requests.append(request.url)
                                route.abort()
                            elif parsed.path.lower().endswith(("/submit", "/mock-run", "/apply")):
                                submission_requests.append((request.method, request.url))
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_fixture_only)
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.reload(wait_until="domcontentloaded")
                        expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                        # Select a real saved search before starting a local discovery run.
                        expect(page.get_by_label("Saved search profile", exact=True).locator(
                            "option[value='fixture-search']")).to_have_count(1)
                        page.get_by_label("Saved search profile", exact=True).select_option("fixture-search")
                        before = page.request.get(
                            origin + "/profiles/fixture/search-history?limit=50")
                        self.assertEqual(before.status, 200, before.text())
                        before_body = before.json()
                        self.assertEqual(before_body["profile_id"], "fixture")
                        before_ids = {item["id"] for item in before_body["items"]}

                        request_id = "browser-search-history-fixture-run"
                        queued = page.request.post(origin + "/discovery", data={
                            "profile_id": "fixture",
                            "search_profile_id": "fixture-search",
                            "query": "Systems Administrator",
                            "filters": {"remote": "REQUIRED", "min_salary": 65000},
                            "request_id": request_id,
                        })
                        self.assertEqual(queued.status, 202, queued.text())
                        operation_id = queued.json()["operation_id"]
                        self.assertTrue(operation_id)

                        with patch.dict(os.environ, {
                            "MJOBS_DATABASE_URL": store.url,
                            "MJOBS_MOCK_ORIGIN": source_origin,
                            "MJOBS_OUTPUT_DIR": str(root),
                            "MJOBS_OPERATION_MODE": "FIXTURE",
                        }):
                            worker_result = execute_operation(operation_id)
                        self.assertEqual(worker_result["status"], "COMPLETED", worker_result)
                        with store.engine.connect() as connection:
                            operation = connection.execute(select(m.operations).where(
                                m.operations.c.id == operation_id)).mappings().one()
                        self.assertEqual(operation["state"], "COMPLETED")

                        history_path = "/profiles/fixture/search-history"
                        history_response = page.request.get(origin + history_path + "?limit=50")
                        self.assertEqual(history_response.status, 200, history_response.text())
                        history_body = history_response.json()
                        self.assertEqual(history_body["profile_id"], "fixture")
                        new_runs = [item for item in history_body["items"]
                                    if item["id"] not in before_ids]
                        self.assertTrue(new_runs, history_body)
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == history_path):
                            page.get_by_role("button", name="Activity", exact=True).click()
                        panel = page.get_by_role("region", name="Search history", exact=True)
                        expect(panel.get_by_role("heading", name="Search history", exact=True)).to_be_visible()
                        expect(panel).to_contain_text("Read-only search history")
                        table = panel.get_by_role("table", name="Saved search runs for profile fixture")
                        expect(table).to_be_visible()
                        for heading in ("Source", "Query", "Started", "Finished", "Run status",
                                        "Results", "New jobs", "Duplicates", "Errors",
                                        "Filters and details"):
                            expect(table.get_by_role("columnheader", name=heading, exact=True)).to_be_visible()

                        self.assertTrue(all(item["profile_id"] == "fixture" for item in new_runs))

                        required_fields = {
                            "id", "profile_id", "source", "query", "filters", "started_at",
                            "finished_at", "status", "results", "new_jobs", "duplicates", "errors",
                        }
                        for item in new_runs:
                            self.assertTrue(required_fields.issubset(item), item)
                            self.assertEqual(item["source"], "mock-fixture", item)
                            self.assertEqual(item["query"], "Systems Administrator")
                            self.assertIsInstance(item["filters"], dict)
                            self.assertEqual(item["filters"]["remote"], "REQUIRED")
                            self.assertEqual(item["filters"]["min_salary"], 65000)
                            self.assertEqual(item["filters"]["include_titles"], ["Systems Administrator"])
                            started = datetime.fromisoformat(item["started_at"].replace("Z", "+00:00"))
                            finished = datetime.fromisoformat(item["finished_at"].replace("Z", "+00:00"))
                            self.assertIsNotNone(started.utcoffset())
                            self.assertIsNotNone(finished.utcoffset())
                            self.assertLessEqual(started, finished)
                            self.assertEqual(item["status"], "COMPLETED")
                            for counter in ("results", "new_jobs"):
                                self.assertIsInstance(item[counter], int)
                                self.assertGreaterEqual(item[counter], 0)
                            self.assertIsNone(item["duplicates"])
                            self.assertIsInstance(item["errors"], list)

                            row = table.locator("tbody tr").filter(has_text=item["source"]).filter(
                                has_text=item["query"] or "No query recorded")
                            expect(row).to_contain_text(item["status"])
                            for counter in ("results", "new_jobs"):
                                expect(row).to_contain_text(str(item[counter]))
                            expect(row).to_contain_text("Unknown")
                            expect(row).to_contain_text(str(len(item["errors"])))
                            self.assertEqual(row.locator("time").count(), 2)
                            details = row.get_by_text("View filters and run details", exact=True)
                            details.click()
                            expect(row).to_contain_text("Filters:")
                            for title in item["filters"].get("include_titles", []):
                                expect(row).to_contain_text(title)

                        # History must stay usable inside the Activity panel on mobile.
                        page.set_viewport_size({"width": 390, "height": 844})
                        overflow = page.evaluate("""() => ({
                            width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
                            outside: [...document.querySelectorAll('main *')]
                                .filter(el => el.getBoundingClientRect().right > innerWidth)
                                .slice(0, 12).map(el => ({tag: el.tagName, class: el.className,
                                    right: el.getBoundingClientRect().right}))
                        })""")
                        self.assertLessEqual(overflow["scrollWidth"], overflow["width"], overflow)
                        scroller = panel.get_by_role("region", name="Scrollable search history", exact=True)
                        self.assertTrue(scroller.evaluate("el => el.scrollWidth > el.clientWidth"))
                        scroller.focus()
                        page.keyboard.press("ArrowRight")
                        page.wait_for_function("() => document.querySelector('.table-scroll').scrollLeft > 0")
                        table.get_by_role("columnheader", name="Filters and details", exact=True).scroll_into_view_if_needed()
                        self.assertTrue(scroller.evaluate("el => el.scrollLeft > 0"))
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                        page.set_viewport_size({"width": 1280, "height": 720})

                        # A full reload must read the same completed run from the API.
                        page.reload(wait_until="domcontentloaded")
                        expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == history_path):
                            page.get_by_role("button", name="Activity", exact=True).click()
                        reloaded_panel = page.get_by_role("region", name="Search history", exact=True)
                        reloaded_table = reloaded_panel.get_by_role(
                            "table", name="Saved search runs for profile fixture")
                        expect(reloaded_table).to_be_visible()
                        reloaded_body = page.request.get(origin + history_path + "?limit=50").json()
                        self.assertEqual({item["id"] for item in reloaded_body["items"]},
                                         {item["id"] for item in history_body["items"]})
                        for item in new_runs:
                            expect(reloaded_table.locator("tbody tr").filter(
                                has_text=item["source"]).filter(
                                    has_text=item["query"] or "No query recorded")).to_be_visible()

                        # The other profile has no runs; it must not inherit this profile's rows.
                        other_path = "/profiles/other/search-history"
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == other_path):
                            page.get_by_label("Active profile", exact=True).select_option("other")
                        expect(page.get_by_label("Active profile", exact=True)).to_have_value("other")
                        other_response = page.request.get(origin + other_path + "?limit=50")
                        self.assertEqual(other_response.status, 200, other_response.text())
                        other_body = other_response.json()
                        self.assertEqual(other_body["profile_id"], "other")
                        self.assertEqual(other_body["items"], [])
                        other_panel = page.get_by_role("region", name="Search history", exact=True)
                        expect(other_panel.get_by_text(
                            "No search history for this profile yet.", exact=True)).to_be_visible()
                        expect(other_panel).not_to_contain_text(request_id)
                        self.assertEqual(external_requests, [])
                        self.assertEqual(submission_requests, [])
                        self.assertEqual(page_errors, [])
                        context.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    server_thread.join(timeout=10)
                    sock.close()
                    store.engine.dispose()
                self.assertFalse(server_thread.is_alive(), "Search-history API server did not stop")


if __name__ == "__main__":
    unittest.main()
