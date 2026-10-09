"""Connected, local-fixture acceptance for the first-run search workflow."""
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from httpx import MockTransport, Response
from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class FirstRunWizardBrowserTests(unittest.TestCase):
    def test_wizard_runs_profile_scoped_saved_search_and_reads_back_history(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-first-run-wizard-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'wizard.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile({
                **dict(PROFILE),
                "facts": [{
                    "id": "pending-imported-fact",
                    "kind": "experience",
                    "text": "Supported a synthetic Linux server environment.",
                    "source": "synthetic uploaded resume",
                    "confidence": 90,
                    "verified": False,
                    "user_approved": False,
                }],
            })
            source_id = configure_source(store, "fixture", provider="greenhouse")
            save_search_profile(store, {
                "id": "wizard-search",
                "profile_id": "fixture",
                "name": "Remote Python engineer roles",
                "enabled": True,
                "include_titles": ["Python Engineer"],
                "remote": "ANY",
                "employment_types": [],
                "source_ids": [source_id],
            })

            with fixture_server() as source_origin:
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                app = create_app(store, source_origin, root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                server_thread = threading.Thread(target=server.run,
                                                 kwargs={"sockets": [sock]}, daemon=True)
                external_requests = []
                application_requests = []
                collect_bodies = []
                worker_results = []
                source_requests = []
                page_errors = []

                synthetic_job = {
                    "id": 82021,
                    "title": "Python Engineer",
                    "location": {"name": "Remote - United States"},
                    "content": "<p>Build and support Python services.</p>",
                    "updated_at": "2026-10-01T12:00:00Z",
                    "absolute_url": "https://job-boards.greenhouse.io/fixture/jobs/82021",
                }

                def synthetic_greenhouse_transport(request):
                    source_requests.append(request.url.path)
                    if request.url.path.endswith("/jobs"):
                        return Response(200, json={"jobs": [synthetic_job], "meta": {"total": 1}})
                    return Response(200, json={"name": "Synthetic Greenhouse Employer"})

                def collect_with_synthetic_provider(current_store, current_source_id,
                                                    current_profile_id, **kwargs):
                    return collect_source(
                        current_store, current_source_id, current_profile_id,
                        transport=MockTransport(synthetic_greenhouse_transport), **kwargs)
                try:
                    server_thread.start()
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not server_thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Isolated first-run API did not start")
                        time.sleep(0.02)

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context()

                        def local_only(route):
                            request = route.request
                            parsed = urlsplit(request.url)
                            if parsed.netloc != urlsplit(origin).netloc:
                                external_requests.append(request.url)
                                route.abort()
                            elif parsed.path.lower().endswith(("/submit", "/mock-run", "/apply")):
                                application_requests.append((request.method, request.url))
                                route.abort()
                            else:
                                route.continue_()

                        def run_fixture_collection(route):
                            request = route.request
                            collect_bodies.append(request.post_data_json)
                            response = route.fetch()
                            if response.status == 202:
                                operation_id = response.json()["operation_id"]
                                with patch.dict(os.environ, {
                                    "MJOBS_DATABASE_URL": store.url,
                                    "MJOBS_MOCK_ORIGIN": source_origin,
                                    "MJOBS_OUTPUT_DIR": str(root),
                                    "MJOBS_OPERATION_MODE": "FIXTURE",
                                }):
                                    with patch("workers.tasks.collect_source",
                                               side_effect=collect_with_synthetic_provider):
                                        worker_results.append(execute_operation(operation_id))
                            route.fulfill(response=response)

                        context.route("**/*", local_only)
                        context.route(origin + "/sources/**/collect", run_fixture_collection)
                        page = context.new_page()
                        page.set_default_timeout(12000)
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.reload(wait_until="domcontentloaded")

                        expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard).to_be_visible()
                        expect(wizard.get_by_role("listitem")).to_have_count(10)
                        expect(wizard).to_contain_text("External submission stays off")
                        fact_step = wizard.get_by_role("listitem").nth(2)
                        expect(fact_step).to_contain_text("Needs attention")
                        expect(fact_step).to_contain_text(
                            "Review and approve imported facts before MJobs uses them in tailored documents.")
                        fact_step.get_by_role("button", name="Review facts", exact=True).click()
                        expect(page.get_by_role("button", name="Profile", exact=True)).to_have_attribute(
                            "aria-current", "page")
                        # Scope by the imported fact's text so approvals cannot be
                        # inherited from another career fact.
                        pending_fact = page.locator("fieldset.fact").filter(
                            has_text="Supported a synthetic Linux server environment.")
                        expect(pending_fact).to_contain_text("Pending")
                        expect(pending_fact.get_by_label("I verified this fact")).not_to_be_checked()
                        expect(pending_fact.get_by_label("I approve its use in documents")).not_to_be_checked()
                        expect(page.get_by_role("button", name="Submit application", exact=True)).to_have_count(0)
                        pending_fact.get_by_label("I verified this fact").check()
                        pending_fact.get_by_label("I approve its use in documents").check()
                        with page.expect_response(lambda response:
                                response.request.method == "PUT"
                                and response.url == origin + "/profiles") as profile_saved:
                            page.get_by_role("button", name="Save profile", exact=True).click()
                        self.assertEqual(profile_saved.value.status, 200, profile_saved.value.text())
                        profile_readback = page.request.get(origin + "/profiles/fixture")
                        self.assertEqual(profile_readback.status, 200, profile_readback.text())
                        saved_fact = next((item for item in profile_readback.json()["facts"]
                                           if item.get("fact_id", item.get("id")) == "pending-imported-fact"), None)
                        self.assertIsNotNone(saved_fact, profile_readback.text())
                        self.assertTrue(saved_fact["verified"])
                        self.assertTrue(saved_fact["user_approved"])
                        page.reload(wait_until="domcontentloaded")
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard.get_by_role("listitem").nth(2).get_by_text(
                            "Complete", exact=True)).to_be_visible()
                        wizard.get_by_role("button", name="Open profile", exact=True).click()
                        expect(page.get_by_role("button", name="Profile", exact=True)).to_have_attribute("aria-current", "page")
                        wizard.get_by_role("button", name="Open resume library", exact=True).click()
                        expect(page.get_by_role("button", name="Profile", exact=True)).to_have_attribute("aria-current", "page")
                        wizard.get_by_role("button", name="Continue without a resume", exact=True).click()
                        expect(wizard.get_by_role("listitem").nth(1).get_by_text("Complete", exact=True)).to_be_visible()
                        wizard.get_by_role("button", name="Open saved searches", exact=True).click()
                        expect(page.get_by_role("button", name="Sources", exact=True)).to_have_attribute("aria-current", "page")
                        expect(page.get_by_role("heading", name="Scheduled search profiles", exact=True)).to_be_visible()
                        page.get_by_role("button", name=re.compile(r"^Jobs")).click()
                        automation_step = wizard.get_by_role("listitem").nth(8)
                        automation_step.get_by_role("button", name="Keep manual review only").click()
                        expect(automation_step.get_by_text("Complete", exact=True)).to_be_visible()
                        page.reload(wait_until="domcontentloaded")
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard.get_by_role("listitem").nth(8).get_by_text("Complete", exact=True)).to_be_visible()
                        source = wizard.get_by_label("Enabled source for this search", exact=True)
                        source.select_option(source_id)
                        search = wizard.get_by_label("Saved search profile", exact=True)
                        search.select_option("wizard-search")

                        wizard.get_by_role("button", name="Run saved search", exact=True).click()
                        last_step = wizard.get_by_role("listitem").nth(9)
                        expect(last_step.get_by_text("Complete", exact=True)).to_be_visible(timeout=20000)

                        self.assertEqual(len(collect_bodies), 1, collect_bodies)
                        self.assertEqual(collect_bodies[0]["profile_id"], "fixture")
                        self.assertEqual(collect_bodies[0]["search_profile_id"], "wizard-search")
                        self.assertEqual(len(worker_results), 1)
                        self.assertEqual(worker_results[0]["status"], "COMPLETED", worker_results)
                        history_response = page.request.get(origin + "/profiles/fixture/search-history?limit=1")
                        self.assertEqual(history_response.status, 200, history_response.text())
                        history = history_response.json()
                        self.assertEqual(history["profile_id"], "fixture")
                        self.assertEqual(history["items"][0]["status"], "COMPLETED")
                        self.assertEqual(history["items"][0]["filters"]["include_titles"], ["Python Engineer"])
                        self.assertEqual(history["items"][0]["results"], 1)
                        self.assertEqual(set(source_requests), {
                            "/v1/boards/fixture", "/v1/boards/fixture/jobs",
                        })
                        page.reload(wait_until="domcontentloaded")
                        wizard = page.get_by_role("region", name="First-run setup")
                        page.get_by_role("button", name=re.compile(r"^Jobs")).click()
                        expect(page.locator(".job-card").filter(
                            has_text="Python Engineer")).to_be_visible()
                        wizard.get_by_role("button", name="Set up later", exact=True).click()
                        resume = page.get_by_role("button", name=re.compile(r"^Resume first-run setup"))
                        expect(resume).to_be_visible()
                        page.reload(wait_until="domcontentloaded")
                        expect(resume).to_be_visible()
                        resume.click()
                        resumed_wizard = page.get_by_role("region", name="First-run setup")
                        expect(resumed_wizard).to_be_visible()
                        expect(resumed_wizard.get_by_role("listitem").nth(9).get_by_text("Complete", exact=True)).to_be_visible()
                        self.assertEqual(external_requests, [])
                        self.assertEqual(application_requests, [])
                        self.assertEqual(page_errors, [])
                        browser.close()
                finally:
                    server.should_exit = True
                    server_thread.join(timeout=5)
                    store.engine.dispose()
                    self.assertFalse(server_thread.is_alive(), "API server did not stop cleanly")
