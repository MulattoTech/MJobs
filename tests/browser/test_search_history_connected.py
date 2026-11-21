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
from services.documents.durable_comparison import queue_comparison
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.uploads import save_resume
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
                "enabled": True,
                "include_titles": ["Systems Administrator"],
                "remote": "REQUIRED",
                "employment_types": [],
                "required_keywords": ["Python"],
                "salary_min": 65000,
                "salary_period": "YEAR",
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
                        page.get_by_label("Included job titles", exact=True).fill("Python Developer")
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == "/search-profiles"
                                and response.request.method == "PUT") as saved:
                            page.get_by_role("button", name="Save title and keyword filters", exact=True).click()
                        self.assertEqual(saved.value.status, 200)
                        expect(page.get_by_role("status").filter(
                            has_text="Saved title and keyword filters")).to_be_visible()
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_label("Saved search profile", exact=True).select_option("fixture-search")
                        expect(page.get_by_label("Included job titles", exact=True)).to_have_value("Python Developer")
                        before = page.request.get(
                            origin + "/profiles/fixture/search-history?limit=50")
                        self.assertEqual(before.status, 200, before.text())
                        before_body = before.json()
                        self.assertEqual(before_body["profile_id"], "fixture")
                        before_ids = {item["id"] for item in before_body["items"]}

                        page.get_by_role("button", name="Sources", exact=True).click()
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == "/discovery"
                                and response.request.method == "POST") as queued_response:
                            page.get_by_role("button", name="Collect local fixture jobs", exact=True).click()
                        queued = queued_response.value
                        self.assertEqual(queued.status, 202, queued.text())
                        self.assertEqual(queued.request.post_data_json["search_profile_id"], "fixture-search")
                        request_id = queued.request.post_data_json["request_id"]
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
                        expect(page.get_by_text(
                            "Local fixture jobs collected for your saved profile. No employer was contacted.",
                            exact=True)).to_be_visible()
                        expect(page.locator(".job-card").filter(has_text="Python Developer")).to_have_count(1)
                        expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)
                        with store.engine.connect() as connection:
                            operation = connection.execute(select(m.operations).where(
                                m.operations.c.id == operation_id)).mappings().one()
                        self.assertEqual(operation["state"], "COMPLETED")

                        # Admit a valid owned comparison through the production queue contract.
                        application_id = prepare(store, store.jobs()[0]["id"], "fixture", root,
                                                 tailor_resume=True)
                        target = next(row for row in application_documents(store, application_id)
                                      if row["kind"] == "resume")
                        uploaded = save_resume(store, root, "fixture", "synthetic-base.txt",
                                               "text/plain", b"Alex Fixture\nSkills\nSQL; Python\n")
                        queued_comparison = queue_comparison(
                            store, root, "fixture", application_id, uploaded["revision_id"],
                            target["id"], "browser-mobile-active-comparison",
                        )
                        active_operation_id = queued_comparison["operation_id"]
                        self.assertEqual(queued_comparison["state"], "QUEUED")

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
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == "/operations"):
                            page.get_by_role("button", name="Refresh activity", exact=True).click()
                        expect(page.locator(".operation").filter(
                            has_text="UPLOADED_RESUME_COMPARE_QUEUED")).to_have_count(1)
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
                            self.assertEqual(item["query"], "")
                            self.assertIsInstance(item["filters"], dict)
                            self.assertEqual(item["filters"]["remote"], "REQUIRED")
                            self.assertEqual(item["filters"]["salary_min"], 65000)
                            self.assertEqual(item["filters"]["salary_period"], "YEAR")
                            self.assertEqual(item["filters"]["include_titles"], ["Python Developer"])
                            self.assertEqual(item["results"], 4)
                            self.assertEqual(item["new_jobs"], 1)
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
                        active_row = page.locator(".operation").filter(
                            has_text="UPLOADED_RESUME_COMPARE_QUEUED")
                        expect(active_row).to_have_count(1)
                        expect(active_row).to_contain_text("QUEUED")
                        cancel = active_row.get_by_role("button", name="Cancel", exact=True)
                        expect(cancel).to_be_visible()
                        expect(cancel).to_be_enabled()
                        bounds = cancel.bounding_box()
                        self.assertIsNotNone(bounds)
                        self.assertGreaterEqual(bounds["x"], 0)
                        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
                        label = active_row.locator("strong")
                        self.assertLessEqual(label.evaluate("el => el.getBoundingClientRect().right"), 390)
                        self.assertLessEqual(label.evaluate("el => el.scrollWidth"),
                                             active_row.evaluate("el => el.clientWidth"))
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == "/operations/" + active_operation_id + "/cancel") as cancelled:
                            cancel.click()
                        self.assertEqual(cancelled.value.status, 202)
                        expect(active_row).to_contain_text("CANCELLED")
                        expect(active_row.get_by_role("button", name="Cancel", exact=True)).to_have_count(0)
                        with store.engine.connect() as connection:
                            persisted = connection.execute(select(m.operations).where(
                                m.operations.c.id == active_operation_id)).mappings().one()
                        self.assertEqual(persisted["state"], "CANCELLED")
                        self.assertTrue(persisted["cancel_requested"])
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
                        expect(page.locator(".operation").filter(
                            has_text="UPLOADED_RESUME_COMPARE_QUEUED")).to_contain_text("CANCELLED")
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
                        expect(page.locator(".operation").filter(
                            has_text="UPLOADED_RESUME_COMPARE_QUEUED")).to_have_count(0)
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


def test_backend_valid_source_labels_survive_api_history_readback_and_reload(record_property):
    """Read synthetic stored labels through the real API and compiled SPA."""
    import json
    from time import monotonic, sleep

    from services.discovery.search_history import record_search_history
    from services.discovery.search_history_events import build_search_history_record

    labels = {
        "ascii_201": "A" * 201,
        "ascii_256": "B" * 256,
        "mixed_256": "C" * 128 + "\U0001f680" * 128,
    }
    receipt = {
        "outcome": "NOT_COMPLETED",
        "fixture_admitted": False,
        "fixture_history_records": 0,
        "fixture_writes_before_read_baseline": True,
        "producer_trim_preserved_labels": False,
        "source_lengths": {
            name: {"code_points": len(label),
                   "utf16_units": len(label.encode("utf-16-le")) // 2}
            for name, label in labels.items()
        },
        "phases": [],
        "database_unchanged": False,
        "history_unchanged": False,
        "applications_unchanged": False,
        "attempts_unchanged": False,
        "operations_unchanged": False,
        "request_count": 0,
        "request_limit_exceeded": False,
        "external_requests": 0,
        "unexpected_mutations": [],
        "page_error_types": [],
    }
    cleanup = {
        "context_closed": True, "browser_closed": True,
        "playwright_stopped": True, "api_thread_stopped": True,
        "socket_closed": True, "database_disposed": True,
        "temporary_directory_removed": True,
        "error_types": [], "all_resources_closed": False,
        "primary_failure_preserved": False,
    }
    directory = store = sock = server = server_thread = None
    playwright = browser = context = None
    primary_failure = None
    try:
        assert (ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance"
        directory = tempfile.TemporaryDirectory(prefix="mjobs-history-source-labels-")
        root = Path(directory.name)
        cleanup["temporary_directory_removed"] = False
        store = Store(f"sqlite:///{(root / 'history.db').as_posix()}")
        cleanup["database_disposed"] = False
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(dict(PROFILE))
        store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                            "email": "other@example.test"})

        expected = {"fixture": {}, "other": {}}
        fixture_labels = [("fixture", name, label) for name, label in labels.items()]
        fixture_labels.append(("other", "other_profile", "Other fixture source"))
        for profile_id, name, label in fixture_labels:
            # This deliberately seeds terminal metadata; no discovery worker runs.
            run = build_search_history_record({
                "id": f"mj236-{name}", "kind": "DISCOVER", "entity_id": profile_id,
                "state": "COMPLETED", "attempt_count": 1, "params": {},
                "started_at": "2026-11-14T00:00:00Z",
                "updated_at": "2026-11-14T00:00:01Z",
            }, {
                "source": f"  {label}  ", "query": f"MJ236 {name}",
                "filters": {"remote": "REQUIRED"},
                "results": 3, "new_jobs": 1, "duplicates": 2, "errors": [],
            })
            assert run is not None and run["source"] == label
            saved = record_search_history(store, run)
            assert saved["source"] == label
            expected[profile_id][saved["run_id"]] = {
                "id": saved["run_id"],
                **{key: value for key, value in saved.items() if key != "run_id"},
            }
        receipt["fixture_history_records"] = len(fixture_labels)
        receipt["producer_trim_preserved_labels"] = True

        sock = bound_browser_socket()
        cleanup["socket_closed"] = False
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(store, "http://127.0.0.1:1", root,
                         "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        server_thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True,
        )
        cleanup["api_thread_stopped"] = False
        server_thread.start()
        deadline = monotonic() + 10
        while not server.started:
            assert server_thread.is_alive() and monotonic() <= deadline, "History API did not start"
            sleep(0.02)

        def database_snapshot():
            with store.engine.connect() as connection:
                return {
                    "history": [dict(row) for row in connection.execute(
                        select(m.audit).where(m.audit.c.action.in_({
                            "SEARCH_RUN_COMPLETED", "SEARCH_RUN_FAILED", "SEARCH_RUN_CANCELLED",
                        })).order_by(m.audit.c.id)).mappings()],
                    **{
                        name: [dict(row) for row in connection.execute(
                            select(table).order_by(table.c.id)).mappings()]
                        for name, table in (
                            ("applications", m.applications),
                            ("attempts", m.attempts),
                            ("operations", m.operations),
                        )
                    },
                }

        before = database_snapshot()
        assert len(before["history"]) == 4
        assert before["applications"] == before["attempts"] == before["operations"] == []
        receipt["fixture_admitted"] = True

        playwright = sync_playwright().start()
        cleanup["playwright_stopped"] = False
        browser = playwright.chromium.launch(headless=True)
        cleanup["browser_closed"] = False
        context = browser.new_context()
        cleanup["context_closed"] = False

        def local_reads_only(route):
            request = route.request
            parsed = urlsplit(request.url)
            receipt["request_count"] += 1
            if receipt["request_count"] > 500:
                receipt["request_limit_exceeded"] = True
                route.abort()
            elif (parsed.scheme, parsed.netloc) != ("http", urlsplit(origin).netloc):
                receipt["external_requests"] += 1
                route.abort()
            elif request.method != "GET":
                if len(receipt["unexpected_mutations"]) < 20:
                    receipt["unexpected_mutations"].append(
                        {"method": request.method, "path": parsed.path})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", local_reads_only)
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: receipt["page_error_types"].append(type(error).__name__))
        page.goto(origin, wait_until="domcontentloaded")
        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

        def verify_history(response, profile_id, phase):
            assert response.request.method == "GET"
            assert response.url == f"{origin}/profiles/{profile_id}/search-history?limit=50"
            assert response.status == 200
            body = response.json()
            assert body["profile_id"] == profile_id and body["next_cursor"] is None
            assert len(body["items"]) == len(expected[profile_id])
            assert {item["id"]: item for item in body["items"]} == expected[profile_id]
            panel = page.get_by_role("region", name="Search history", exact=True)
            table = panel.get_by_role(
                "table", name=f"Saved search runs for profile {profile_id}", exact=True)
            expect(table).to_be_visible()
            expect(table.locator("tbody tr")).to_have_count(len(expected[profile_id]))
            for item in expected[profile_id].values():
                row = table.locator("tbody tr").filter(
                    has=page.get_by_role("cell", name=item["query"], exact=True))
                expect(row).to_have_count(1)
                # text_content preserves the exact label, including non-BMP characters.
                assert row.get_by_role("cell").nth(0).text_content() == item["source"]
            receipt["phases"].append({
                "name": phase, "profile_id": profile_id, "status": response.status,
                "items": len(body["items"]), "api_labels_exact": True, "ui_labels_exact": True,
            })
            return table

        history_url = origin + "/profiles/fixture/search-history?limit=50"
        with page.expect_response(lambda response:
                response.url == history_url and response.request.method == "GET") as initial:
            page.get_by_role("button", name="Activity", exact=True).click()
        verify_history(initial.value, "fixture", "initial_readback")

        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
        with page.expect_response(lambda response:
                response.url == history_url and response.request.method == "GET") as reloaded:
            page.get_by_role("button", name="Activity", exact=True).click()
        verify_history(reloaded.value, "fixture", "reload")

        other_url = origin + "/profiles/other/search-history?limit=50"
        with page.expect_response(lambda response:
                response.url == other_url and response.request.method == "GET") as other:
            page.get_by_label("Active profile", exact=True).select_option("other")
        expect(page.get_by_label("Active profile", exact=True)).to_have_value("other")
        other_table = verify_history(other.value, "other", "profile_isolation")
        for label in labels.values():
            expect(other_table.get_by_role("cell", name=label, exact=True)).to_have_count(0)

        after = database_snapshot()
        for name in ("history", "applications", "attempts", "operations"):
            receipt[f"{name}_unchanged"] = after[name] == before[name]
            assert receipt[f"{name}_unchanged"], name
        receipt["database_unchanged"] = after == before
        assert receipt["database_unchanged"]
        assert not receipt["request_limit_exceeded"]
        assert receipt["external_requests"] == 0
        assert receipt["unexpected_mutations"] == []
        assert receipt["page_error_types"] == []
        receipt["outcome"] = "PASS"
    except BaseException as error:
        primary_failure = error
        receipt["outcome"] = "FAIL"
        receipt["failure_type"] = type(error).__name__
        raise
    finally:
        for key, resource in (("context_closed", context), ("browser_closed", browser)):
            if resource is not None:
                try:
                    resource.close()
                    cleanup[key] = True
                except Exception as error:
                    cleanup["error_types"].append(type(error).__name__)
        if playwright is not None:
            try:
                playwright.stop()
                cleanup["playwright_stopped"] = True
            except Exception as error:
                cleanup["error_types"].append(type(error).__name__)
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            try:
                if server_thread.ident is not None:
                    server_thread.join(timeout=10)
                cleanup["api_thread_stopped"] = not server_thread.is_alive()
            except Exception as error:
                cleanup["error_types"].append(type(error).__name__)
        if sock is not None:
            try:
                sock.close()
                cleanup["socket_closed"] = sock.fileno() == -1
            except Exception as error:
                cleanup["error_types"].append(type(error).__name__)
        if cleanup["api_thread_stopped"]:
            if store is not None:
                try:
                    store.engine.dispose()
                    cleanup["database_disposed"] = True
                except Exception as error:
                    cleanup["error_types"].append(type(error).__name__)
            if directory is not None:
                try:
                    directory.cleanup()
                    cleanup["temporary_directory_removed"] = not Path(directory.name).exists()
                except Exception as error:
                    cleanup["error_types"].append(type(error).__name__)
        cleanup["primary_failure_preserved"] = primary_failure is not None
        cleanup["all_resources_closed"] = not cleanup["error_types"] and all(
            cleanup[key] for key in (
                "context_closed", "browser_closed", "playwright_stopped",
                "api_thread_stopped", "socket_closed", "database_disposed",
                "temporary_directory_removed",
            )
        )
        if not cleanup["all_resources_closed"] and primary_failure is None:
            receipt["outcome"] = "FAIL_CLEANUP"
        record_property("search_history_source_label_receipt", json.dumps(receipt, sort_keys=True))
        record_property("search_history_source_label_cleanup", json.dumps(cleanup, sort_keys=True))
        if not cleanup["all_resources_closed"] and primary_failure is None:
            raise AssertionError("Search-history source-label resources did not close")



if __name__ == "__main__":
    unittest.main()
