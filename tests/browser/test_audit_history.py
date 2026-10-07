"""The Activity tab reads safe audit summaries for only the selected profile."""
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
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from fixtures.mock_server import fixture_server
from services.applications.mock_adapter import apply_mock
from services.documents.generate import prepare
from services.discovery.pipeline import discover
from services.discovery.search_profiles import save_search_profile
from services.tracking.applications import add_application_note, set_application_pipeline_status
from workers.dispatch import enqueue_operation
from workers.operations import claim
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


class AuditHistoryBrowserTests(unittest.TestCase):
    def test_activity_history_is_profile_scoped_and_survives_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-audit-history-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant",
                                "email": "other@example.invalid"})
            save_search_profile(store, {
                "id": "browser-audit-search-profile",
                "profile_id": "fixture",
                "name": "Synthetic saved search",
                "include_titles": ["Private synthetic search phrase"],
            })
            with fixture_server() as fixture_origin:
                discover(store, fixture_origin, "fixture")
                application_id = prepare(store, store.jobs("fixture")[0]["id"],
                                         "fixture", root)
                submitted = apply_mock(store, application_id, fixture_origin, root,
                                       submit=True)
                self.assertEqual(submitted["state"], "CONFIRMED")
            preparation = enqueue_operation(store, "PREPARE", "fixture", {
                "job_id": store.jobs("fixture")[0]["id"]})
            claim(store, preparation)
            set_application_pipeline_status(store, "fixture", application_id,
                                            "APPLIED", "browser-audit-status")
            add_application_note(store, "fixture", application_id,
                                 "Private synthetic recruiter note", "browser-audit-note")
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "browser-audit-merge-company", "name": "Browser Merge Employer",
                    "domain": "browser-merge.example",
                })
            first_source = upsert_company_job_source(
                store, company_id="browser-audit-merge-company", provider="rss",
                tenant_identifier="rss-board", public_api_url="https://browser-merge.example/rss.json",
                collection_strategy="PUBLIC_JSON",
            )
            second_source = upsert_company_job_source(
                store, company_id="browser-audit-merge-company", provider="greenhouse",
                tenant_identifier="gh-board", public_api_url="https://browser-merge.example/greenhouse.json",
                collection_strategy="PUBLIC_JSON",
            )
            application_url = "https://browser-merge.example/careers/apply/req-1"
            for source, provider, tenant, external_id in (
                (first_source, "rss", "rss-board", "rss-req-1"),
                (second_source, "greenhouse", "gh-board", "gh-req-1"),
            ):
                import_company_feed_jobs(
                    store, company_id="browser-audit-merge-company", source_id=source["id"],
                    provider=provider, tenant=tenant, endpoint=source["endpoint"],
                    strategy="PUBLIC_JSON", profile_id="fixture",
                    items=[{"id": external_id, "title": "Systems Administrator",
                            "description": "Manage synthetic systems", "location": "Remote",
                            "url": application_url}],
                )
            with store.engine.begin() as connection:
                connection.execute(m.audit.insert().values(
                    id="other-history", entity_id="other", action="PROFILE_SAVED",
                    payload={"value": "other-private-setting"},
                    timestamp="2026-10-07T10:01:00+00:00"))
            app = create_app(store, "http://127.0.0.1:1", ROOT,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated audit-history API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    page.get_by_label("Job scoring provider", exact=True).select_option("ollama")
                    page.get_by_label("Job scoring model", exact=True).fill("llama3.2:3b")
                    page.get_by_role("button", name="Save AI task settings", exact=True).click()
                    expect(page.get_by_text("AI task settings saved for this profile.", exact=True)).to_be_visible()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    expect(history).to_contain_text("A profile setting changed")
                    expect(history).to_contain_text("Job added to this profile's search")
                    expect(history).to_contain_text("Job match scored")
                    expect(history).to_contain_text("Application status reported by you")
                    expect(history).to_contain_text("Application note added")
                    expect(history).to_contain_text("Application preparation queued")
                    expect(history).to_contain_text("Application preparation started")
                    expect(history).to_contain_text("Application submitted to the local mock ATS")
                    expect(history).to_contain_text("Fictional mock application outcome confirmed")
                    expect(history).to_contain_text(
                        "New source posting attached to an existing job")
                    expect(history).not_to_contain_text(application_url)
                    expect(history).not_to_contain_text("Private synthetic recruiter note")
                    expect(history).not_to_contain_text("job_scoring")
                    expect(history).not_to_contain_text("ollama")
                    expect(history).not_to_contain_text("llama3.2:3b")
                    before_search_setting_ids = page.evaluate("""async () => {
                        const response = await fetch('/profiles/fixture/audit-history');
                        const history = await response.json();
                        return history.events.filter(event => event.type === 'SETTING_CHANGED')
                            .map(event => event.id);
                    }""")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    page.get_by_role("button", name="Pause", exact=True).click()
                    expect(page.get_by_role("button", name="Resume", exact=True)).to_be_visible()
                    after_search_settings = page.evaluate("""async () => {
                        const response = await fetch('/profiles/fixture/audit-history');
                        const history = await response.json();
                        return history.events.filter(event => event.type === 'SETTING_CHANGED')
                            .map(event => event.id);
                    }""")
                    new_search_setting_ids = [event_id for event_id in after_search_settings
                                              if event_id not in before_search_setting_ids]
                    self.assertEqual(len(new_search_setting_ids), 1)
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    expect(history).to_contain_text("A profile setting changed")
                    expect(history).not_to_contain_text("Private synthetic search phrase")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    reloaded_history = page.get_by_role("list", name="Audit history")
                    expect(reloaded_history).to_contain_text("A profile setting changed")
                    self.assertEqual(reloaded_history.locator("li").filter(
                        has_text="A profile setting changed").count(),
                        len(before_search_setting_ids) + 1)
                    expect(reloaded_history).not_to_contain_text("other-private-setting")
                    expect(reloaded_history).to_contain_text("Application status reported by you")
                    expect(reloaded_history).to_contain_text(
                        "Application submitted to the local mock ATS")
                    expect(reloaded_history).to_contain_text(
                        "New source posting attached to an existing job")
                    page.get_by_role("button", name="Profile", exact=True).click()
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    other_history = page.get_by_role("list", name="Audit history")
                    expect(other_history).to_contain_text("Profile details updated")
                    expect(other_history).not_to_contain_text("A profile setting changed")
                    expect(other_history).not_to_contain_text("Application status reported by you")
                    expect(other_history).not_to_contain_text(
                        "Application submitted to the local mock ATS")
                    expect(other_history).not_to_contain_text(
                        "Fictional mock application outcome confirmed")
                    expect(other_history).not_to_contain_text(
                        "New source posting attached to an existing job")
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()
