"""Connected acceptance for the editable remote and Texoma saved-search presets."""
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
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SearchProfileSeedBrowserTests(unittest.TestCase):
    def test_job_finder_presets_save_and_reload_as_independent_searches(self):
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

                    remote_name, local_name = "Remote IT / Infrastructure", "Texoma Local IT / Support"
                    page.get_by_role("button", name=f"Use {remote_name}", exact=True).click()
                    expect(page.get_by_label("Search name", exact=True)).to_have_value(remote_name)
                    expect(page.get_by_label("Remote work eligible state")).to_have_value("OK")
                    expect(page.get_by_role("combobox", name="Remote preference", exact=True)).to_have_value("REQUIRED")
                    expect(page.get_by_role("combobox", name="Employment type", exact=True)).to_have_value("")

                    expect(page.get_by_role("button", name="Save search", exact=True)).to_be_enabled()
                    page.get_by_role("button", name="Save search", exact=True).click()
                    remote_schedule = page.get_by_role("region", name=f"Schedule “{remote_name}”")
                    expect(remote_schedule).to_be_visible()
                    expect(remote_schedule.get_by_label("Enable scheduled discovery", exact=True)).not_to_be_checked()

                    persisted = page.request.get(
                        origin + "/search-profiles", params={"profile_id": profile_id})
                    self.assertEqual(persisted.status, 200, persisted.text())
                    saved, = [row for row in persisted.json()["search_profiles"]
                              if row["name"] == remote_name]
                    self.assertIn("Infrastructure Manager", saved["criteria"]["include_titles"])
                    self.assertEqual(saved["criteria"]["remote"], "REQUIRED")
                    self.assertEqual(saved["criteria"]["remote_residence_state"], "OK")
                    self.assertEqual(saved["criteria"]["employment_types"], [])
                    self.assertFalse(saved["criteria"]["scheduler"]["enabled"])
                    self.assertEqual(saved["source_ids"], [])

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(remote_schedule).to_be_visible()
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

                    page.get_by_role("button", name="Sources", exact=True).click()
                    page.get_by_role("button", name=f"Use {local_name}", exact=True).click()
                    states = page.get_by_label("Allowed states")
                    cities = page.get_by_label("Allowed cities")
                    expect(cities).to_have_value("Durant\nDenison\nSherman")
                    states.fill("OK")
                    states.press("End")
                    states.press("Enter")
                    state_draft = states.input_value()
                    states.press_sequentially("tx")
                    cities.press("Control+End")
                    cities.press("Enter")
                    cities.press_sequentially("Oklahoma ")
                    city_draft = cities.input_value()
                    cities.press_sequentially("City")
                    page.get_by_role("button", name="Save search", exact=True).click()
                    expect(page.get_by_role("region", name=f"Schedule “{local_name}”")).to_be_visible()

                    save_search_profile(store, {
                        "id": saved["id"], "profile_id": profile_id, "name": remote_name,
                        "employment_types": ["FULL_TIME", "CONTRACT"],
                    })
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    remote_card = page.locator(".document").filter(
                        has=page.get_by_text(remote_name, exact=True))
                    remote_card.get_by_role("button", name="Edit search", exact=True).click()
                    expect(page.get_by_role("combobox", name="Employment type", exact=True)).to_have_value("FULL_TIME")
                    renamed = "Remote infrastructure reviewed"
                    page.get_by_label("Search name", exact=True).fill(renamed)
                    page.get_by_label("Remote work eligible state").fill("TX")
                    with page.expect_response(lambda response:
                            response.request.method == "PUT" and
                            urlsplit(response.url).path == "/search-profiles") as edit:
                        page.get_by_role("button", name="Update search", exact=True).click()
                    self.assertEqual(edit.value.status, 200, edit.value.text())

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_role("region", name=f"Schedule “{renamed}”")).to_be_visible()
                    readback = page.request.get(origin + "/search-profiles", params={"profile_id": profile_id})
                    self.assertEqual(readback.status, 200, readback.text())
                    profiles = {row["name"]: row for row in readback.json()["search_profiles"]}
                    self.assertEqual(profiles[renamed]["criteria"]["remote_residence_state"], "TX")
                    with self.subTest("unedited employment list"):
                        self.assertEqual(profiles[renamed]["criteria"]["employment_types"],
                                         ["FULL_TIME", "CONTRACT"])
                    local = profiles[local_name]["criteria"]
                    self.assertIsNone(local["remote_residence_state"])
                    with self.subTest("geographic drafts and readback"):
                        self.assertEqual((state_draft, city_draft), ("OK\n", "Durant\nDenison\nSherman\nOklahoma "))
                        self.assertEqual(local["states"], ["OK", "TX"])
                        self.assertEqual(local["cities"], ["Durant", "Denison", "Sherman", "Oklahoma City"])
                    self.assertCountEqual(local["work_arrangements"], ["ONSITE", "HYBRID"])
                    self.assertFalse(local["scheduler"]["enabled"])

                    first_source = configure_source(store, "fixture-board-one")
                    second_source = configure_source(store, "fixture-board-two")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    local_card = page.locator(".document").filter(has=page.get_by_text(local_name, exact=True))
                    picker = local_card.get_by_label(f"Collection source for {local_name}")
                    expect(local_card.get_by_role("button", name="Collect for this search")).to_be_disabled()
                    page_errors = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    for source_id, name in ((first_source, "fixture-board-one"),
                                            (second_source, "fixture-board-two")):
                        picker.select_option(source_id)
                        expect(picker).to_have_value(source_id)
                        expect(local_card.get_by_role("button", name=f"Collect {local_name} from {name}")).to_be_enabled()
                    picker.select_option("")
                    expect(local_card.get_by_role("button", name="Collect for this search")).to_be_disabled()
                    self.assertEqual(page_errors, [])

                    page.get_by_role("button", name="Jobs 0", exact=True).click()
                    saved_search = page.get_by_label("Saved search profile", exact=True).last
                    saved_search.select_option("")
                    page.get_by_label("Saved search name", exact=True).fill("Any title filters")
                    with page.expect_response(lambda response:
                            response.request.method == "PUT" and
                            urlsplit(response.url).path == "/search-profiles") as saved_any:
                        page.get_by_role("button", name="Save current filters", exact=True).click()
                    self.assertEqual(saved_any.value.status, 200)
                    any_option = saved_search.locator("option", has_text="Any title filters")
                    expect(any_option).to_have_count(1)
                    any_search_id = any_option.get_attribute("value")
                    any_url = f"{origin}/search-profiles/{any_search_id}"
                    any_before = page.request.get(any_url).json()
                    self.assertEqual(any_before["criteria"]["include_titles"], [])

                    page.get_by_role("button", name="Sources", exact=True).click()
                    any_card = page.locator(".document").filter(
                        has=page.get_by_text("Any title filters", exact=True))
                    any_card.get_by_role("button", name="Edit search", exact=True).click()
                    include_titles = page.get_by_label("Include titles", exact=True)
                    expect(include_titles).to_have_value("")
                    page.get_by_label("Search name", exact=True).fill("Renamed any title filters")
                    with page.expect_response(lambda response:
                            response.request.method == "PUT" and
                            urlsplit(response.url).path == "/search-profiles", timeout=5000) as renamed_any:
                        page.get_by_role("button", name="Update search", exact=True).click()
                    self.assertEqual(renamed_any.value.status, 200)
                    any_after = page.request.get(any_url).json()
                    self.assertEqual(any_after["name"], "Renamed any title filters")
                    self.assertEqual(any_after["criteria"]["include_titles"], [])
                    self.assertEqual(any_after["criteria"]["job_filters"], any_before["criteria"]["job_filters"])
                    self.assertEqual(any_after["enabled"], any_before["enabled"])
                    expect(page.get_by_role("button", name="Save search", exact=True)).to_be_visible()
                    self.assertTrue(include_titles.evaluate("field => field.validity.valueMissing"))

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
