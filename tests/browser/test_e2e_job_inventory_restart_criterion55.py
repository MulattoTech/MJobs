"""Connected restart acceptance for MJ-REQ-292 criterion 55."""
from copy import deepcopy
import json
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
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    (ROOT / "apps/web/dist/index.html").is_file(),
    "Build apps/web before browser acceptance",
)
class JobInventoryRestartCriterion55BrowserTests(unittest.TestCase):
    def _start_api(self, store, fixture_origin, output_root):
        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(
            create_app(
                store,
                fixture_origin,
                output_root,
                "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            ),
            log_level="error",
            access_log=False,
        ))
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                self.fail("Isolated criterion-55 API did not start")
            time.sleep(0.02)
        return origin, sock, server, thread

    def _stop_api(self, store, sock, server, thread):
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        store.engine.dispose()
        self.assertFalse(thread.is_alive(), "Isolated criterion-55 API did not stop")

    def _browser(self, playwright, origin, profile_id, external, errors):
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 1000})
        local_netloc = urlsplit(origin).netloc

        def local_only(route):
            if urlsplit(route.request.url).netloc != local_netloc:
                external.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(10_000)
        page.add_init_script(
            f"localStorage.setItem('mjobs-profile', {json.dumps(profile_id)})"
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin, wait_until="domcontentloaded")
        expect(page.get_by_label("Active profile", exact=True)).to_have_value(profile_id)
        return browser, context, page

    def _request_json(self, request, url):
        response = request.get(url)
        self.assertEqual(response.status, 200, response.text())
        return response.json()

    def _inventory(self, request, origin, profile_id):
        listing = self._request_json(
            request,
            f"{origin}/jobs?profile_id={profile_id}&sort=score",
        )["jobs"]
        normalized = []
        for row in sorted(listing, key=lambda item: item["requisition"]):
            detail = self._request_json(
                request,
                f"{origin}/jobs/{row['id']}?profile_id={profile_id}",
            )
            postings = [{
                "id": posting["id"],
                "source": posting["source"],
                "source_job_id": posting["source_job_id"],
                "source_url": posting["source_url"],
                "application_url": posting["application_url"],
            } for posting in detail["postings"]]
            routes = []
            for route in detail["routes"]:
                resolution = route.get("direct_employer_resolution")
                routes.append({
                    "id": route["id"],
                    "provider": route["provider"],
                    "tenant": route["tenant"],
                    "requisition": route["requisition"],
                    "url": route["url"],
                    "authority": route["authority"],
                    "preferred_by_profile": route["preferred_by_profile"],
                    "direct_employer_resolution": ({
                        "status": resolution["status"],
                        "source_route_id": resolution["source_route_id"],
                        "candidate_route_ids": sorted(resolution["candidate_route_ids"]),
                    } if resolution else None),
                })
            normalized.append({
                "id": row["id"],
                "canonical_job_id": row["canonical_job_id"],
                "company_id": row["company_id"],
                "company_name": row["company_name"],
                "requisition": row["requisition"],
                "title": row["title"],
                "match_score": row["match_score"],
                "posting_count": row["posting_count"],
                "posting_sources": sorted(row["posting_sources"]),
                "provider": row["provider"],
                "application_url": row["application_url"],
                "postings": sorted(postings, key=lambda item: item["source_job_id"]),
                "routes": sorted(routes, key=lambda item: item["id"]),
            })
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return normalized, encoded

    def _assert_fixture_inventory(self, inventory):
        self.assertEqual([row["requisition"] for row in inventory], ["A", "B"])
        self.assertEqual([row["title"] for row in inventory], [
            "Python Developer",
            "Data Engineer",
        ])
        self.assertEqual([row["match_score"] for row in inventory], [100, 100])
        self.assertEqual([row["posting_count"] for row in inventory], [3, 1])
        copies = {
            row["requisition"]: {
                posting["source_job_id"] for posting in row["postings"]
            }
            for row in inventory
        }
        self.assertEqual(copies, {
            "A": {"board-A", "board-A-syndicated", "employer-A"},
            "B": {"employer-B"},
        })
        route_providers = {
            row["requisition"]: {route["provider"] for route in row["routes"]}
            for row in inventory
        }
        self.assertEqual(route_providers, {
            "A": {"mock-board", "mock-ats"},
            "B": {"mock-ats"},
        })
        self.assertEqual(
            len({row["id"] for row in inventory}),
            len(inventory),
        )
        posting_ids = [
            posting["id"]
            for row in inventory
            for posting in row["postings"]
        ]
        self.assertEqual(len(posting_ids), len(set(posting_ids)))

    def _assert_jobs_ui(self, page):
        cards = page.locator(".job-card")
        expect(cards).to_have_count(2)
        python_card = cards.filter(has_text="Python Developer")
        data_card = cards.filter(has_text="Data Engineer")
        expect(python_card).to_be_visible()
        expect(data_card).to_be_visible()
        expect(python_card).to_contain_text("100%")
        expect(python_card).to_contain_text("3 postings")
        expect(data_card).to_contain_text("100%")
        expect(data_card).to_contain_text("1 posting")

    def test_jobs_and_source_copies_survive_full_stack_restart(self):
        with tempfile.TemporaryDirectory(
            prefix="mjobs-criterion55-inventory-",
        ) as directory, fixture_server() as fixture_origin:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'criterion55.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            discover(store, fixture_origin, "fixture")
            second_profile = deepcopy(PROFILE)
            second_profile.update(
                id="criterion55-second-profile",
                name="Second Synthetic Profile",
                email="second@example.test",
            )
            store.save_profile(second_profile)
            self.assertEqual(store.count("canonical_jobs"), 2)
            self.assertEqual(store.count("job_postings"), 4)

            external, errors = [], []
            first_origin, sock, server, thread = self._start_api(
                store,
                fixture_origin,
                root,
            )
            try:
                with sync_playwright() as playwright:
                    browser, context, page = self._browser(
                        playwright,
                        first_origin,
                        "fixture",
                        external,
                        errors,
                    )
                    self._assert_jobs_ui(page)
                    before, before_bytes = self._inventory(
                        context.request,
                        first_origin,
                        "fixture",
                    )
                    self._assert_fixture_inventory(before)
                    browser.close()
            finally:
                self._stop_api(store, sock, server, thread)

            reopened = Store(database_url)
            reopened.bind_workspace_mode("FIXTURE")
            second_origin, sock, server, thread = self._start_api(
                reopened,
                fixture_origin,
                root,
            )
            self.assertNotEqual(first_origin, second_origin)
            try:
                with sync_playwright() as playwright:
                    browser, context, page = self._browser(
                        playwright,
                        second_origin,
                        "fixture",
                        external,
                        errors,
                    )
                    self._assert_jobs_ui(page)
                    after, after_bytes = self._inventory(
                        context.request,
                        second_origin,
                        "fixture",
                    )
                    self._assert_fixture_inventory(after)
                    self.assertEqual(after_bytes, before_bytes)
                    self.assertEqual(reopened.count("canonical_jobs"), 2)
                    self.assertEqual(reopened.count("job_postings"), 4)

                    second_jobs = self._request_json(
                        context.request,
                        second_origin
                        + "/jobs?profile_id=criterion55-second-profile&sort=score",
                    )["jobs"]
                    self.assertEqual(
                        {row["requisition"] for row in second_jobs},
                        {"A", "B"},
                    )
                    self.assertEqual(
                        {row["match_score"] for row in second_jobs},
                        {None},
                    )
                    for row in second_jobs:
                        detail = self._request_json(
                            context.request,
                            second_origin
                            + f"/jobs/{row['id']}?profile_id=criterion55-second-profile",
                        )
                        self.assertIsNone(detail["match"])
                    browser.close()
            finally:
                self._stop_api(reopened, sock, server, thread)

            self.assertEqual(external, [], external)
            self.assertEqual(errors, [], errors)


if __name__ == "__main__":
    unittest.main()
