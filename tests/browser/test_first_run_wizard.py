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
from services.discovery.sources.collect import collect_source
from tests.browser.test_profile_import import resume_pdf
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
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard).to_be_visible()
                        expect(wizard.get_by_role("listitem")).to_have_count(10)
                        expect(wizard).to_contain_text("External submission stays off")
                        wizard.get_by_role("button", name="Open profile", exact=True).click()
                        expect(page.get_by_role("button", name="Profile", exact=True)).to_have_attribute(
                            "aria-current", "page")
                        page.get_by_label("Full name", exact=True).fill("Example Applicant")
                        page.get_by_label("Email", exact=True).fill("imported@example.invalid")
                        page.locator("label").filter(has_text="Work arrangements").locator("select").select_option("REMOTE")
                        page.get_by_role("button", name="Save profile", exact=True).click()
                        expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
                        profile_id = page.get_by_label("Active profile", exact=True).input_value()
                        self.assertTrue(profile_id)
                        self.assertEqual(store.profile(profile_id)["facts"], [])

                        wizard.get_by_role("button", name="Open resume library", exact=True).click()
                        page.get_by_label("Choose a resume (up to 10 MiB)").set_input_files({
                            "name": "synthetic-first-run.pdf",
                            "mimeType": "application/pdf",
                            "buffer": resume_pdf("Supported synthetic Linux servers for a fictional lab."),
                        })
                        page.get_by_label("Resume label", exact=True).fill("Synthetic first-run resume")
                        page.get_by_role("button", name="Store resume", exact=True).click()
                        resume_row = page.locator("[data-resume-version='1']")
                        expect(resume_row).to_be_visible()
                        resume_row.get_by_role("button", name="Extract and review", exact=True).click()
                        review = page.get_by_role("region", name="Resume extraction review")
                        expect(review).to_be_visible()
                        expect(review.get_by_text(
                            "Supported synthetic Linux servers for a fictional lab.", exact=False
                        )).to_be_visible()
                        expect(review.get_by_label("Add contact email", exact=False)).not_to_be_checked()
                        review.get_by_role("button", name="Add selected to profile editor", exact=True).click()
                        pending_fact = page.locator("fieldset.fact").filter(
                            has_text="Supported synthetic Linux servers for a fictional lab.")
                        expect(pending_fact).to_contain_text("Pending")
                        expect(pending_fact.get_by_label("I verified this fact")).not_to_be_checked()
                        expect(pending_fact.get_by_label("I approve its use in documents")).not_to_be_checked()
                        expect(page.get_by_role("button", name="Submit application", exact=True)).to_have_count(0)
                        for fact in page.locator("fieldset.fact").all():
                            fact.get_by_label("I verified this fact").check()
                            fact.get_by_label("I approve its use in documents").check()
                        with page.expect_response(lambda response:
                                response.request.method == "PUT"
                                and response.url == origin + "/profiles") as profile_saved:
                            page.get_by_role("button", name="Save profile", exact=True).click()
                        self.assertEqual(profile_saved.value.status, 200, profile_saved.value.text())
                        profile_readback = page.request.get(origin + f"/profiles/{profile_id}")
                        self.assertEqual(profile_readback.status, 200, profile_readback.text())
                        imported_facts = profile_readback.json()["facts"]
                        self.assertGreater(len(imported_facts), 0, profile_readback.text())
                        self.assertTrue(all(item["verified"] for item in imported_facts), imported_facts)
                        self.assertTrue(all(item["user_approved"] for item in imported_facts), imported_facts)
                        page.reload(wait_until="domcontentloaded")
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard.get_by_role("listitem").nth(2).get_by_text(
                            "Complete", exact=True)).to_be_visible()
                        wizard.get_by_role("button", name="Open profile", exact=True).click()
                        expect(page.get_by_role("button", name="Profile", exact=True)).to_have_attribute("aria-current", "page")
                        expect(wizard.get_by_role("listitem").nth(1).get_by_text("Complete", exact=True)).to_be_visible()
                        wizard.get_by_role("button", name="Open application preferences", exact=True).click()
                        page.get_by_label("Ask me to choose a route for each job", exact=True).check()
                        page.get_by_role("button", name="Save profile", exact=True).click()
                        expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
                        expect(wizard.get_by_role("listitem").nth(4).get_by_text("Complete", exact=True)).to_be_visible()
                        expect(wizard.get_by_role("listitem").nth(5).get_by_text("Complete", exact=True)).to_be_visible()
                        wizard.get_by_role("button", name="Keep AI disabled", exact=True).click()
                        expect(wizard.get_by_role("listitem").nth(6).get_by_text("Complete", exact=True)).to_be_visible()
                        wizard.get_by_role("button", name="Choose sources", exact=True).click()
                        page.get_by_label("Board token", exact=True).fill("fixture")
                        page.get_by_role("button", name="Add board", exact=True).click()
                        expect(page.get_by_role("status").filter(has_text="Source saved")).to_be_visible()
                        sources_response = page.request.get(origin + "/sources")
                        self.assertEqual(sources_response.status, 200, sources_response.text())
                        source = next(item for item in sources_response.json()["sources"]
                                      if item["tenant"] == "fixture")
                        self.assertTrue(source["enabled"], source)
                        source_id = source["id"]
                        page.reload(wait_until="domcontentloaded")
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard.get_by_role("listitem").nth(7).get_by_text("Complete", exact=True)).to_be_visible()
                        wizard.get_by_role("button", name="Open saved searches", exact=True).click()
                        expect(page.get_by_role("button", name="Sources", exact=True)).to_have_attribute("aria-current", "page")
                        expect(page.get_by_role("heading", name="Scheduled search profiles", exact=True)).to_be_visible()
                        page.get_by_label("Search name", exact=True).fill("Remote Python engineer roles")
                        page.get_by_label("Include titles", exact=True).fill("Python Engineer")
                        search_form = page.locator("form").filter(has=page.get_by_label("Search name", exact=True))
                        search_form.locator("label").filter(has_text="Source").locator("select").select_option(source_id)
                        page.get_by_role("button", name="Save search", exact=True).click()
                        expect(page.get_by_role("status").filter(has_text="Search saved")).to_be_visible()
                        search_response = page.request.get(
                            origin + f"/search-profiles?profile_id={profile_id}")
                        self.assertEqual(search_response.status, 200, search_response.text())
                        saved_search = next(item for item in search_response.json()["search_profiles"]
                                            if item["name"] == "Remote Python engineer roles")
                        page.reload(wait_until="domcontentloaded")
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard.get_by_role("listitem").nth(3).get_by_text("Complete", exact=True)).to_be_visible()
                        automation_step = wizard.get_by_role("listitem").nth(8)
                        automation_step.get_by_role("button", name="Keep manual review only").click()
                        expect(automation_step.get_by_text("Complete", exact=True)).to_be_visible()
                        page.reload(wait_until="domcontentloaded")
                        wizard = page.get_by_role("region", name="First-run setup")
                        expect(wizard.get_by_role("listitem").nth(8).get_by_text("Complete", exact=True)).to_be_visible()
                        source = wizard.get_by_label("Enabled source for this search", exact=True)
                        source.select_option(source_id)
                        search = wizard.get_by_label("Saved search profile", exact=True)
                        search.select_option(saved_search["id"])

                        wizard.get_by_role("button", name="Run saved search", exact=True).click()
                        last_step = wizard.get_by_role("listitem").nth(9)
                        expect(last_step.get_by_text("Complete", exact=True)).to_be_visible(timeout=20000)
                        for step in wizard.get_by_role("listitem").all():
                            expect(step.get_by_text("Complete", exact=True)).to_be_visible()

                        self.assertEqual(len(collect_bodies), 1, collect_bodies)
                        self.assertEqual(collect_bodies[0]["profile_id"], profile_id)
                        self.assertEqual(collect_bodies[0]["search_profile_id"], saved_search["id"])
                        self.assertEqual(len(worker_results), 1)
                        self.assertEqual(worker_results[0]["status"], "COMPLETED", worker_results)
                        history_response = page.request.get(origin + f"/profiles/{profile_id}/search-history?limit=1")
                        self.assertEqual(history_response.status, 200, history_response.text())
                        history = history_response.json()
                        self.assertEqual(history["profile_id"], profile_id)
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
