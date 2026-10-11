"""Connected acceptance for the editable remote infrastructure saved-search example."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SearchProfileSeedBrowserTests(unittest.TestCase):
    def test_remote_infrastructure_example_saves_and_reloads_as_real_profile(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-search-profile-seed-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile_id = "remote-infrastructure-seed-profile"
            store.save_profile({**PROFILE, "id": profile_id, "name": "Seed Fixture",
                                "email": "seed@example.test"})
            self.assertEqual(store.count("job_sources"), 0)
            store.engine.dispose()
            store = Store(database_url)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated search-profile seed API did not start")
                    time.sleep(0.02)

                external = []
                requests = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    context.on("request", lambda request: requests.append(
                        (request.method, request.url)))
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate(f"localStorage.setItem('mjobs-profile', {profile_id!r})")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value(profile_id)
                    page.get_by_role("button", name="Sources", exact=True).click()

                    # Test oracle: this button is absent before the search-profile seed is built.
                    example_button = page.get_by_role(
                        "button", name="Use remote IT infrastructure example", exact=True)
                    expect(example_button).to_be_visible(timeout=500)
                    example_button.click()
                    expect(page.get_by_label("Search name", exact=True)).to_have_value(
                        "Remote Infrastructure / SysAdmin")
                    expected_titles = [
                        "System Administrator", "Systems Administrator", "Windows Administrator",
                        "IT Administrator", "Infrastructure Support", "Endpoint Administrator",
                        "Microsoft 365 Administrator", "Systems Engineer",
                        "Technical Support Engineer", "NOC Engineer",
                    ]
                    expect(page.get_by_role("textbox", name="Include titles", exact=True)).to_have_value(
                        "\n".join(expected_titles))
                    expect(page.get_by_role("combobox", name="Remote preference", exact=True)).to_have_value("REQUIRED")
                    expect(page.get_by_role("combobox", name="Employment type", exact=True)).to_have_value("FULL_TIME")

                    expect(page.get_by_role("button", name="Save search", exact=True)).to_be_enabled()
                    page.get_by_role("button", name="Save search", exact=True).click()
                    expect(page.get_by_role("region", name="Schedule “Remote Infrastructure / SysAdmin”")).to_be_visible()
                    schedule = page.get_by_role("region", name="Schedule “Remote Infrastructure / SysAdmin”")
                    expect(schedule.get_by_label("Enable scheduled discovery", exact=True)).not_to_be_checked()

                    persisted = page.request.get(
                        origin + "/search-profiles", params={"profile_id": profile_id})
                    self.assertEqual(persisted.status, 200, persisted.text())
                    saved, = [row for row in persisted.json()["search_profiles"]
                              if row["name"] == "Remote Infrastructure / SysAdmin"]
                    self.assertEqual(saved["criteria"]["include_titles"], expected_titles)
                    self.assertEqual(saved["criteria"]["remote"], "REQUIRED")
                    self.assertEqual(saved["criteria"]["employment_types"], ["FULL_TIME"])
                    self.assertFalse(saved["criteria"]["scheduler"]["enabled"])
                    self.assertEqual(saved["source_ids"], [])

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_role("region", name="Schedule “Remote Infrastructure / SysAdmin”")).to_be_visible()
                    page.get_by_role("button", name="Jobs 0", exact=True).click()
                    # The onboarding wizard has its own saved-search selector above the Jobs controls.
                    saved_search = page.get_by_label("Saved search profile", exact=True).last
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == "/jobs" and
                            parse_qs(urlsplit(response.url).query).get("search_profile_id") == [saved["id"]]):
                        saved_search.select_option(saved["id"])
                    expect(saved_search).to_have_value(saved["id"])
                    self.assertTrue(any(
                        method == "GET" and urlsplit(url).path == "/jobs" and
                        parse_qs(urlsplit(url).query).get("search_profile_id") == [saved["id"]]
                        for method, url in requests
                    ), f"Selecting the saved seed should run discovery using its persisted search profile: {requests}")
                    self.assertEqual(external, [])
                    self.assertFalse(any("/collect" in urlsplit(url).path for _, url in requests))
                    self.assertEqual(store.count("worker_operations"), 0)
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    server.force_exit = True
                store.engine.dispose()
