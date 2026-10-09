"""Connected acceptance for the profile-scoped optional technology preference seed."""
from pathlib import Path
import json
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import unittest

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class TechnologySkillSeedBrowserTests(unittest.TestCase):
    def test_optional_technology_seed_saves_advisory_preferences_and_reloads(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-technology-skill-seed-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile_id = "technology-seed-profile"
            store.save_profile({**PROFILE, "id": profile_id, "name": "Technology Seed",
                                "email": "technology-seed@example.test"})
            configure_source(store, "technology-seed-source", provider="greenhouse", enabled=True,
                             jitter_seconds=0)
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
                        self.fail("Isolated technology seed API did not start")
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

                    # Test oracle: the documented default technology options are currently absent.
                    preferences = page.get_by_role("region", name="Optional technology preferences")
                    expect(preferences).to_be_visible(timeout=500)
                    seed = preferences.get_by_role(
                        "button", name="Use default technology seed", exact=True)
                    seed.click()
                    skill_names = [
                        "Active Directory", "Windows Server", "PowerShell", "Microsoft 365",
                        "Entra ID", "Azure", "Group Policy", "VMware", "Hyper-V", "DNS",
                        "DHCP", "Intune", "SCCM", "Rubrik", "SAN", "NAS",
                    ]
                    self.assertEqual(preferences.get_by_role("checkbox").count(), len(skill_names))
                    for name in skill_names:
                        expect(preferences.get_by_role("checkbox", name=name, exact=True)).to_be_checked()
                    preferences.get_by_role("checkbox", name="Active Directory", exact=True).uncheck()
                    preferences.get_by_role("checkbox", name="SCCM", exact=True).uncheck()

                    page.get_by_label("Search name", exact=True).fill("Optional technology seed")
                    page.get_by_role("textbox", name="Include titles", exact=True).fill(
                        "Systems Administrator")
                    page.get_by_role("button", name="Save search", exact=True).click()
                    expect(page.get_by_role("region", name="Schedule “Optional technology seed”")).to_be_visible()
                    schedule = page.get_by_role("region", name="Schedule “Optional technology seed”")
                    expect(schedule.get_by_label("Enable scheduled discovery", exact=True)).not_to_be_checked()

                    persisted = page.request.get(
                        origin + "/search-profiles", params={"profile_id": profile_id})
                    self.assertEqual(persisted.status, 200, persisted.text())
                    saved, = [row for row in persisted.json()["search_profiles"]
                              if row["name"] == "Optional technology seed"]
                    self.assertEqual(saved["criteria"]["optional_skills"], [
                        "WINDOWS_SERVER", "POWERSHELL", "MICROSOFT_365", "ENTRA_ID", "AZURE",
                        "GROUP_POLICY", "VMWARE", "HYPER_V", "DNS", "DHCP", "INTUNE", "RUBRIK",
                        "SAN", "NAS",
                    ])
                    self.assertEqual(saved["criteria"]["required_skills"], [])
                    self.assertEqual(saved["criteria"]["required_keywords"], [])
                    self.assertFalse(saved["criteria"]["scheduler"]["enabled"])

                    page.get_by_role("button", name="Pause search", exact=True).click()
                    paused = page.request.get(
                        origin + "/search-profiles", params={"profile_id": profile_id})
                    paused_profile = next(row for row in paused.json()["search_profiles"]
                                          if row["id"] == saved["id"])
                    self.assertEqual(paused_profile["criteria"]["optional_skills"],
                                     saved["criteria"]["optional_skills"])
                    page.get_by_role("button", name="Resume search", exact=True).click()
                    resumed = page.request.get(
                        origin + "/search-profiles", params={"profile_id": profile_id})
                    resumed_profile = next(row for row in resumed.json()["search_profiles"]
                                           if row["id"] == saved["id"])
                    self.assertEqual(resumed_profile["criteria"]["optional_skills"],
                                     saved["criteria"]["optional_skills"])

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    search_summary = page.get_by_text(
                        "Optional technology preferences: Windows Server, PowerShell, Microsoft 365, "
                        "Entra ID, Azure, Group Policy, VMware, Hyper-V, DNS, DHCP, Intune, Rubrik, SAN, NAS",
                        exact=True)
                    expect(search_summary).to_be_visible()
                    page.get_by_role("button", name="Jobs 0", exact=True).click()
                    saved_search = page.get_by_label("Saved search profile", exact=True).last
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == "/jobs" and
                            parse_qs(urlsplit(response.url).query).get("search_profile_id") == [saved["id"]]):
                        saved_search.select_option(saved["id"])

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
