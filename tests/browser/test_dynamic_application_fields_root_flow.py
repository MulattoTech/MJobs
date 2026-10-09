"""Connected browser acceptance for profile-scoped dynamic form review drafts."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.applications.form_analysis import analyze_questionnaire
from services.applications.mock_adapter import _include_question_shape
from services.applications.preflight import build_dynamic_form_snapshot, persist_preflight
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class DynamicApplicationFieldsRootFlowTests(unittest.TestCase):
    def test_prepared_mock_application_supports_local_dynamic_review_and_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-dynamic-form-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            application = store.application(application_id)
            questionnaire = {
                "provider": "mock-ats", "tenant": "example", "requisition": "A",
                "revision": 1,
                "questions": [
                    {"id": "country", "label": "Country", "tag": "select",
                     "required": True, "options": ["United States", "Canada"]},
                    {"id": "region", "label": "State or province", "tag": "select",
                     "required": True, "condition": {"question_id": "country",
                                                           "equals": "United States"},
                     "options": ["Oklahoma", "Texas"],
                     "options_by": {"question_id": "country", "choices": {
                         "United States": ["Oklahoma"], "Canada": ["Ontario"]}}},
                    {"id": "authorization", "label": "Social Security number", "tag": "input",
                     "input_type": "text", "required": False},
                    {"id": "note", "label": "Additional details", "tag": "input",
                     "input_type": "text", "required": False,
                     "optional_section_id": "additional"},
                    {"id": "history", "label": "Work history", "tag": "input",
                     "input_type": "text", "required": False, "repeatable": True,
                     "repeatable_group": "experience", "repeatable_min_rows": 1,
                     "repeatable_max_rows": 3, "repeatable_group_required": False,
                     "repeatable_group_visible": True},
                ],
            }
            report = {
                "result": "UNKNOWN", "reason": "DYNAMIC_REQUIRED_FIELDS_REVIEW_REQUIRED",
                "observed_at": "2026-10-09T00:00:00Z",
                "profile_version": application["profile_version"],
                "document_version": application["document_version"],
                "route_id": application["route_id"], "metadata_fingerprint": "e" * 64,
                "mappings": [],
            }
            _include_question_shape(report, questionnaire,
                                    analyze_questionnaire(questionnaire))
            self.assertIsNotNone(build_dynamic_form_snapshot(report), repr(report))
            persist_preflight(store, application_id, report)

            app = create_app(store, "http://127.0.0.1:1", root,
                             operation_mode="FIXTURE")
            with TestClient(app) as api_test:
                response = api_test.get(
                    f"/profiles/fixture/applications/{application_id}/dynamic-fields")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertGreaterEqual(len(response.json()["fields"]), 4, response.text)
                self.assertTrue(response.json()["fields"][0]["supported"], response.text)
                self.assertEqual(response.json()["optional_sections"], [
                    {"id": "additional", "field_ids": ["note"]},
                ], response.text)
                self.assertEqual(response.json()["repeatable_groups"], [{
                    "id": "experience", "field_ids": ["history"],
                    "minimum_rows": 1, "maximum_rows": 3,
                }], response.text)
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            requests, outside, page_errors = [], [], []
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated dynamic-form API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser_profile = root / "browser-profile"
                    context = playwright.chromium.launch_persistent_context(
                        str(browser_profile), headless=True)
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")
                    origin_netloc = urlsplit(origin).netloc

                    def local_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        requests.append((request.method, parsed.path))
                        if parsed.netloc != origin_netloc:
                            outside.append(request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    page.locator(".job-card").filter(has_text=job["title"]).click()

                    expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()
                    connected = page.get_by_role(
                        "region", name="Dynamic application form review", exact=True)
                    form = page.get_by_role("region", name="Dynamic application form", exact=True)
                    expect(form).to_be_visible()
                    self.assertIn("Country", form.inner_text(),
                                  f"Dynamic fields unavailable: {form.inner_text()}; requests={requests}")
                    country = form.get_by_role("group", name="Country").get_by_label(
                        "Country", exact=True)
                    region = form.get_by_role("group", name="State or province").get_by_label(
                        "State or province", exact=True)
                    expect(region).not_to_be_visible()
                    country.select_option("United States")
                    expect(region).to_be_visible()
                    expect(region.locator("option")).to_have_count(2)
                    region.select_option("Oklahoma")
                    expect(form).to_contain_text("This field needs separate manual review")
                    expect(form).not_to_contain_text("PRIVATE_ANSWER")

                    page.get_by_label("Show optional section: Optional section additional",
                                      exact=True).check()
                    note = form.get_by_role("group", name="Additional details").get_by_label(
                        "Additional details", exact=True)
                    note.fill("Synthetic reviewed note")
                    form.get_by_label("Review this answer for Additional details", exact=True).check()
                    history_group = form.get_by_role("group", name="Work history")
                    history_group.get_by_role("button", name="Add Work history").click()
                    history_group.get_by_label("Work history 1", exact=True).fill("Synthetic role")
                    history_group.get_by_label("Review Work history 1", exact=True).check()

                    form.get_by_label("Review this answer for Country", exact=True).check()
                    form.get_by_label("Review this answer for State or province", exact=True).check()
                    continue_button = form.get_by_role("button", name="Continue", exact=True)
                    expect(continue_button).to_be_enabled()
                    continue_button.click()
                    expect(connected).to_contain_text("browser tab")
                    stored = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('mjobs.dynamic-form.device.v1:'))")
                    self.assertEqual(stored, [])
                    session_drafts = page.evaluate("Object.keys(sessionStorage).filter(k => k.startsWith('mjobs.dynamic-form.v1:'))")
                    self.assertEqual(len(session_drafts), 1)
                    self.assertEqual(outside, [])
                    self.assertFalse(any(method != "GET" for method, _ in requests), requests)

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    page.locator(".job-card").filter(has_text=job["title"]).click()
                    connected = page.get_by_role("region", name="Dynamic application form review", exact=True)
                    form = page.get_by_role("region", name="Dynamic application form", exact=True)
                    expect(connected.get_by_label("Save reviewed answers on this device", exact=True)).not_to_be_checked()
                    expect(form.get_by_role("group", name="Country").get_by_label(
                        "Country", exact=True)).to_have_value("United States")
                    form.get_by_label("Review this answer for Country", exact=True).check()
                    form.get_by_label("Review this answer for State or province", exact=True).check()
                    form.get_by_label("Review this answer for Additional details", exact=True).check()
                    form.get_by_label("Review Work history 1", exact=True).check()
                    connected.get_by_label("Save reviewed answers on this device", exact=True).check()
                    page.evaluate("""(() => {
                      const nativeSet = Storage.prototype.setItem;
                      window.__failDeviceDraftWrite = true;
                      window.__nativeStorageSet = nativeSet;
                      Storage.prototype.setItem = function(key, value) {
                        if (window.__failDeviceDraftWrite && key.startsWith('mjobs.dynamic-form.device.v1:')) {
                          throw new DOMException('Synthetic quota failure', 'QuotaExceededError');
                        }
                        return window.__nativeStorageSet.call(this, key, value);
                      };
                    })()""")
                    form.get_by_role("button", name="Continue", exact=True).click()
                    expect(connected).to_contain_text("Answers could not be saved with the selected browser storage")
                    expect(form.get_by_role("group", name="Country").get_by_label(
                        "Country", exact=True)).to_have_value("United States")
                    page.evaluate("window.__failDeviceDraftWrite = false")
                    form.get_by_role("button", name="Continue", exact=True).click()
                    expect(connected).to_contain_text("saved on this device")
                    stored = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('mjobs.dynamic-form.device.v1:'))")
                    self.assertEqual(len(stored), 1)
                    stored_value = page.evaluate("localStorage.getItem(Object.keys(localStorage).find(k => k.startsWith('mjobs.dynamic-form.device.v1:')))" )
                    self.assertNotIn("reviewed_fields", json.loads(stored_value))
                    self.assertNotIn("PRIVATE_ANSWER", stored_value)
                    session_drafts = page.evaluate("Object.keys(sessionStorage).filter(k => k.startsWith('mjobs.dynamic-form.v1:'))")
                    self.assertEqual(session_drafts, [])

                    context.close()
                    context = playwright.chromium.launch_persistent_context(
                        str(browser_profile), headless=True)
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")
                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    page.locator(".job-card").filter(has_text=job["title"]).click()
                    connected = page.get_by_role(
                        "region", name="Dynamic application form review", exact=True)
                    expect(connected.get_by_label("Save reviewed answers on this device", exact=True)).to_be_checked()
                    form = page.get_by_role("region", name="Dynamic application form", exact=True)
                    expect(form.get_by_role("group", name="Country").get_by_label(
                        "Country", exact=True)).to_have_value("United States")
                    expect(form.get_by_role("group", name="State or province").get_by_label(
                        "State or province", exact=True)).to_have_value("Oklahoma")
                    expect(form.get_by_role("group", name="Additional details").get_by_label(
                        "Additional details", exact=True)).to_have_value("Synthetic reviewed note")
                    expect(form.get_by_label("Review this answer for Country", exact=True)).not_to_be_checked()
                    clear_button = connected.get_by_role("button", name="Clear saved application answers on this device", exact=True)
                    expect(clear_button).to_be_visible()
                    clear_button.click()
                    expect(connected).to_contain_text("Saved local answers were cleared")
                    form = page.get_by_role("region", name="Dynamic application form", exact=True)
                    expect(form.get_by_role("group", name="Country").get_by_label(
                        "Country", exact=True)).to_have_value("")
                    stored_after_clear = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('mjobs.dynamic-form.device.v1:'))")
                    self.assertEqual(stored_after_clear, [])
                    expect(form).not_to_contain_text("PRIVATE_ANSWER")
                    self.assertEqual(outside, [])
                    self.assertFalse(page_errors, page_errors)
                    self.assertFalse(any(method != "GET" for method, _ in requests), requests)
                    context.close()
            finally:
                server.should_exit = True
                if thread.ident is not None:
                    thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated dynamic-form API must stop")


if __name__ == "__main__":
    unittest.main()
