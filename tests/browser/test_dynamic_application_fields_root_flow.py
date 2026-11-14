"""Connected browser acceptance for profile-scoped dynamic form review drafts."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
import pytest
from sqlalchemy import select
import uvicorn
from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
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


@pytest.mark.skipif(
    not (ROOT / "apps/web/dist/index.html").is_file(),
    reason="Build apps/web before browser acceptance",
)
def test_repeatable_selects_require_current_choices_and_fresh_reviews(
        tmp_path, record_property):
    """Exercise repeated choices through the mounted, local-only root flow."""
    root = tmp_path
    store = Store(f"sqlite:///{(root / 'repeated-select.db').as_posix()}")
    sock = server = thread = playwright = browser = context = None
    requests, outside, page_errors = [], [], []
    receipt = {
        "schema_version": 1, "fixture_admitted": False, "phase": "fixture_setup",
        "phases": [], "outcome": "NOT_COMPLETED",
    }
    cleanup = {"context_closed": False, "browser_closed": False,
               "playwright_stopped": False, "server_stopped": False,
               "store_disposed": False, "errors": []}
    primary_error = None
    try:
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(dict(PROFILE))
        with fixture_server() as fixture:
            discover(store, fixture, "fixture")
        job = store.jobs("fixture")[0]
        application_id = prepare(store, job["id"], "fixture", root)
        application = store.application(application_id)
        choices = {"Operations": ["Support", "Facilities"],
                   "Engineering": ["Platform", "Security"]}
        questionnaire = {
            "provider": "mock-ats", "tenant": "example", "requisition": "A",
            "revision": 1, "questions": [
                {"id": "department", "label": "Department", "tag": "select",
                 "required": True, "options": list(choices)},
                {"id": "team", "label": "Team", "tag": "select",
                 "required": True, "options": sum(choices.values(), []),
                 "options_by": {"question_id": "department", "choices": choices},
                 "repeatable": True, "repeatable_group": "experience",
                 "repeatable_min_rows": 1, "repeatable_max_rows": 3,
                 "repeatable_group_required": True,
                 "repeatable_group_visible": True},
            ],
        }
        report = {
            "result": "UNKNOWN", "reason": "DYNAMIC_REQUIRED_FIELDS_REVIEW_REQUIRED",
            "observed_at": "2026-11-14T00:00:00Z",
            "profile_version": application["profile_version"],
            "document_version": application["document_version"],
            "route_id": application["route_id"], "metadata_fingerprint": "e" * 64,
            "mappings": [],
        }
        _include_question_shape(report, questionnaire, analyze_questionnaire(questionnaire))
        assert build_dynamic_form_snapshot(report) is not None
        persist_preflight(store, application_id, report)
        app = create_app(store, "http://127.0.0.1:1", root, operation_mode="FIXTURE")
        with TestClient(app) as api_test:
            response = api_test.get(
                f"/profiles/fixture/applications/{application_id}/dynamic-fields")
            assert response.status_code == 200, response.text
            metadata = response.json()
        assert [field["id"] for field in metadata["fields"]] == ["department", "team"]
        assert all(field["type"] == "select" and field["supported"] is True
                   and field["manual_review_required"] is False
                   for field in metadata["fields"])
        assert metadata["fields"][1]["options_by"] == choices
        assert metadata["fields"][1]["depends_on"] == "department"
        assert metadata["repeatable_groups"] == [{
            "id": "experience", "field_ids": ["team"],
            "minimum_rows": 1, "maximum_rows": 3,
        }]
        application_before = store.application(application_id)
        with store.engine.connect() as connection:
            attempts_before = list(connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == application_id)).mappings())
        receipt["metadata"] = {
            "fingerprint": metadata["fingerprint"],
            "field_ids": [field["id"] for field in metadata["fields"]],
            "current_choices": choices, "repeatable_groups": metadata["repeatable_groups"],
            "application_state_before": application_before["state"],
            "attempts_before": len(attempts_before),
        }

        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        origin_netloc = urlsplit(origin).netloc
        stale_origin_responses = []
        stale_origin_complete = threading.Event()

        async def observe_stale_response(scope, receive, send):
            # The client cancels unread 409 bodies. Observe the real API's send
            # without replacing, retrying, or changing the browser request.
            expected_query = b"expected_fingerprint=" + metadata["fingerprint"].encode("ascii")
            if not (scope["type"] == "http" and scope["method"] == "GET"
                    and scope["path"] == f"/profiles/fixture/applications/{application_id}/dynamic-fields"
                    and scope.get("query_string", b"") == expected_query
                    and [value for name, value in scope.get("headers", [])
                         if name.lower() == b"host"] == [origin_netloc.encode("ascii")]):
                return await app(scope, receive, send)
            status, observed_body, oversized = None, bytearray(), False

            async def observed_send(message):
                nonlocal status, oversized
                if message["type"] == "http.response.start":
                    status = message["status"]
                elif message["type"] == "http.response.body" and status == 409:
                    chunk = message.get("body", b"")
                    if not oversized and len(observed_body) + len(chunk) <= 4096:
                        observed_body.extend(chunk)
                    else:
                        oversized = True
                await send(message)
                if (message["type"] == "http.response.body" and status == 409
                        and not message.get("more_body", False)):
                    observation = {
                        "method": scope["method"], "path": scope["path"],
                        "host": origin_netloc, "query": expected_query.decode("ascii"),
                        "status": status, "complete": True, "within_limit": not oversized,
                        "body_bytes": len(observed_body) if not oversized else None,
                        "body_sha256": hashlib.sha256(observed_body).hexdigest() if not oversized else None,
                        "code": None, "json_valid": False,
                    }
                    if not oversized:
                        try:
                            payload = json.loads(observed_body)
                            detail = payload.get("detail") if isinstance(payload, dict) else None
                            code = detail.get("code") if isinstance(detail, dict) else None
                            observation["json_valid"] = True
                            if (isinstance(code, str) and 1 <= len(code) <= 64
                                    and all(char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in code)):
                                observation["code"] = code
                        except (ValueError, UnicodeError, RecursionError):
                            pass
                    stale_origin_responses.append(observation)
                    stale_origin_complete.set()

            await app(scope, receive, observed_send)

        server = uvicorn.Server(uvicorn.Config(
            observe_stale_response, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() <= deadline, (
                "Isolated repeated-select API did not start")
            time.sleep(0.02)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

        def local_only(route):
            request = route.request
            parsed = urlsplit(request.url)
            requests.append((request.method, parsed.path))
            if parsed.netloc != origin_netloc or request.method != "GET":
                outside.append({"method": request.method, "path": parsed.path,
                                "same_origin": parsed.netloc == origin_netloc})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(origin, wait_until="domcontentloaded")
        page.get_by_label("Active profile", exact=True).select_option("fixture")
        expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
        draft = {
            "profile_id": "fixture", "application_id": application_id,
            "fingerprint": metadata["fingerprint"],
            "answers": {"department": "Operations", "team": ["Out-of-options"]},
            "enabled_optional_sections": [],
        }
        receipt["restored_draft_answers"] = draft["answers"]
        draft_key = page.evaluate("""draft => {
            const key = 'mjobs.dynamic-form.v1:' + encodeURIComponent(draft.profile_id)
                + ':' + encodeURIComponent(draft.application_id) + ':' + draft.fingerprint;
            sessionStorage.setItem(key, JSON.stringify(draft));
            return key;
        }""", draft)
        page.get_by_role("button", name="Applications", exact=False).click()
        page.locator(".job-card").filter(has_text=job["title"]).click()
        expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()
        connected = page.get_by_role("region", name="Dynamic application form review", exact=True)
        form = page.get_by_role("region", name="Dynamic application form", exact=True)
        expect(form).to_be_visible()
        department = form.get_by_role("group", name="Department", exact=True).get_by_label(
            "Department", exact=True)
        department_review = form.get_by_label("Review this answer for Department", exact=True)
        team_group = form.get_by_role("group", name="Team", exact=True)
        row = team_group.get_by_label("Team 1", exact=True)
        row_review = team_group.get_by_label("Review Team 1", exact=True)
        continue_button = form.get_by_role("button", name="Continue", exact=True)

        def remember(phase):
            receipt["phase"] = phase
            receipt["phases"].append({
                "phase": phase, "department": department.input_value(),
                "department_reviewed": department_review.is_checked(),
                "rows": team_group.locator('[id^="dynamic-application-team-"]').evaluate_all(
                    """elements => elements.map(element => ({
                        tag: element.tagName, value: element.value,
                        options: element.tagName === 'SELECT'
                            ? Array.from(element.options, option => option.value) : null
                    }))"""),
                "row_reviews": team_group.get_by_role("checkbox").evaluate_all(
                    """elements => elements.map(element => ({
                        label: element.getAttribute('aria-label'),
                        checked: element.checked, disabled: element.disabled
                    }))"""),
                "continue_enabled": continue_button.is_enabled(),
            })
            preference_control = form.get_by_role("combobox", name="Additional preference", exact=True)
            if preference_control.count():
                receipt["phases"][-1]["additional_preference"] = {
                    "value": preference_control.input_value(),
                    "reviewed": form.get_by_label(
                        "Review this answer for Additional preference", exact=True).is_checked(),
                }

        expect(department).to_have_value("Operations")
        expect(row).to_have_count(1)
        expect(row).to_be_visible()
        expect(row_review).not_to_be_checked()
        department_review.check()
        if row_review.is_enabled():
            row_review.check()
        receipt["fixture_admitted"] = True
        remember("invalid_restored_choice")
        # This is the intended baseline failure, after real metadata and UI admission.
        expect(continue_button).to_be_disabled()

        remember("native_select_shape")
        assert row.evaluate("element => element.tagName") == "SELECT"
        expect(row.locator("option")).to_have_text(
            ["Select an option", "Support", "Facilities"])
        row.select_option("Support")
        remember("first_current_choice_unreviewed")
        expect(row_review).not_to_be_checked()
        expect(continue_button).to_be_disabled()
        row_review.check()
        remember("first_current_choice_reviewed")
        expect(continue_button).to_be_enabled()

        row.select_option("Facilities")
        remember("choice_edit_invalidates_review")
        expect(row_review).not_to_be_checked()
        expect(continue_button).to_be_disabled()
        row_review.check()
        expect(continue_button).to_be_enabled()
        team_group.get_by_role("button", name="Add Team", exact=True).click()
        row_two = team_group.get_by_label("Team 2", exact=True)
        review_two = team_group.get_by_label("Review Team 2", exact=True)
        remember("added_row_unreviewed")
        expect(row_two).to_have_value("")
        expect(review_two).not_to_be_checked()
        expect(continue_button).to_be_disabled()
        row_two.select_option("Support")
        review_two.check()
        expect(continue_button).to_be_enabled()
        team_group.get_by_role("button", name="Remove Team 1", exact=True).click()
        remember("remove_preserves_remaining_row_review")
        expect(row).to_have_value("Support")
        expect(row_review).to_be_checked()
        expect(row_two).to_have_count(0)
        expect(review_two).to_have_count(0)
        expect(continue_button).to_be_enabled()

        department.select_option("Engineering")
        remember("parent_change_clears_rows_and_reviews")
        expect(department_review).not_to_be_checked()
        expect(row).to_have_count(0)
        expect(row_review).to_have_count(0)
        expect(continue_button).to_be_disabled()
        department_review.check()
        team_group.get_by_role("button", name="Add Team", exact=True).click()
        remember("new_parent_choices_require_new_review")
        expect(row.locator("option")).to_have_text(
            ["Select an option", "Platform", "Security"])
        expect(row).to_have_value("")
        expect(row_review).not_to_be_checked()
        row.select_option("Platform")
        expect(continue_button).to_be_disabled()
        row_review.check()
        remember("new_parent_choice_reviewed")
        expect(continue_button).to_be_enabled()

        continue_button.click()
        expect(connected).to_contain_text(
            "Reviewed answers are saved in this browser tab for this application.")
        saved = json.loads(page.evaluate("key => sessionStorage.getItem(key)", draft_key))
        receipt["saved_answers"] = saved["answers"]
        remember("actual_continue_saved_local_draft")
        assert saved["answers"] == {"department": "Engineering", "team": ["Platform"]}
        assert saved["fingerprint"] == metadata["fingerprint"]
        assert "reviewed_fields" not in saved
        assert page.evaluate("""() => Object.keys(localStorage).filter(
            key => key.startsWith('mjobs.dynamic-form.device.v1:'))""") == []
        application_after = store.application(application_id)
        with store.engine.connect() as connection:
            attempts_after = list(connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == application_id)).mappings())
        receipt["application_unchanged"] = application_after == application_before
        receipt["attempts_unchanged"] = attempts_after == attempts_before
        receipt["metadata"]["attempts_after"] = len(attempts_after)
        assert application_after == application_before
        assert attempts_after == attempts_before

        # Publish a second safe fixture definition after answers have been reviewed.
        # This is the only deliberate fixture mutation in the refresh phase.
        old_draft_bytes = page.evaluate("key => sessionStorage.getItem(key)", draft_key)
        assert json.loads(old_draft_bytes) == saved
        row.select_option("Security")
        expect(row_review).not_to_be_checked()
        row_review.check()
        remember("unsaved_answer_before_metadata_refresh")
        assert page.evaluate("key => sessionStorage.getItem(key)", draft_key) == old_draft_bytes
        receipt["phase"] = "revised_metadata_fixture_admission"
        receipt["metadata_refresh"] = {"fixture_admitted": False,
                                       "old_fingerprint": metadata["fingerprint"]}
        revised_questionnaire = {
            **questionnaire, "revision": 2, "questions": [
                *questionnaire["questions"],
                {"id": "followup", "label": "Additional preference", "tag": "select",
                 "required": True, "options": ["Daytime", "Evening"]},
            ],
        }
        current_application = store.application(application_id)
        revised_report = {
            "result": "UNKNOWN", "reason": "DYNAMIC_REQUIRED_FIELDS_REVIEW_REQUIRED",
            "observed_at": "2026-11-14T00:00:01Z",
            "profile_version": current_application["profile_version"],
            "document_version": current_application["document_version"],
            "route_id": current_application["route_id"],
            "metadata_fingerprint": hashlib.sha256(json.dumps(
                revised_questionnaire, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "mappings": [],
        }
        _include_question_shape(
            revised_report, revised_questionnaire, analyze_questionnaire(revised_questionnaire))
        assert build_dynamic_form_snapshot(revised_report) is not None
        persist_preflight(store, application_id, revised_report)
        fields_path = f"/profiles/fixture/applications/{application_id}/dynamic-fields"
        refreshed_response = page.evaluate("""async path => {
            const response = await fetch(path, { cache: 'no-store' });
            return { status: response.status, body: await response.json() };
        }""", fields_path)
        receipt["metadata_refresh"]["admission_status"] = refreshed_response["status"]
        assert refreshed_response["status"] == 200
        refreshed_metadata = refreshed_response["body"]
        assert refreshed_metadata["fingerprint"] != metadata["fingerprint"]
        assert [field["id"] for field in refreshed_metadata["fields"]] == [
            "department", "team", "followup"]
        new_field = refreshed_metadata["fields"][2]
        assert new_field["required"] is True and new_field["supported"] is True
        assert new_field["type"] == "select" and new_field["manual_review_required"] is False
        assert new_field["options"] == ["Daytime", "Evening"]
        # Compare browser effects to the state after the intentional fixture write.
        application_after_fixture = store.application(application_id)
        with store.engine.connect() as connection:
            attempts_after_fixture = list(connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == application_id)).mappings())
        refresh = {
            "fixture_admitted": True,
            "old_fingerprint": metadata["fingerprint"],
            "new_fingerprint": refreshed_metadata["fingerprint"],
            "new_field_ids": [field["id"] for field in refreshed_metadata["fields"]],
            "new_required_field": new_field["id"],
            "pending_unsaved_team": "Security",
            "application_state_after_fixture": application_after_fixture["state"],
            "attempts_after_fixture": len(attempts_after_fixture),
        }
        receipt["metadata_refresh"] = refresh
        receipt["phase"] = "old_fingerprint_continue"
        with page.expect_response(lambda response: (
                urlsplit(response.url).netloc == origin_netloc
                and urlsplit(response.url).path == fields_path
                and parse_qs(urlsplit(response.url).query).get("expected_fingerprint")
                == [metadata["fingerprint"]])) as rejected:
            continue_button.click()
        stale_response = rejected.value
        refresh["stale_response_status"] = stale_response.status
        refresh["stale_origin_responses"] = stale_origin_responses
        assert stale_origin_complete.wait(8), "The matching origin response did not complete"
        refresh["stale_origin_response_count"] = len(stale_origin_responses)
        assert len(stale_origin_responses) == 1
        origin_response = stale_origin_responses[0]
        assert origin_response["complete"] is True and origin_response["within_limit"] is True
        assert origin_response["json_valid"] is True
        assert (origin_response["method"], origin_response["host"], origin_response["path"],
                origin_response["query"], origin_response["status"]) == (
                    stale_response.request.method, urlsplit(stale_response.url).netloc,
                    urlsplit(stale_response.url).path, urlsplit(stale_response.url).query,
                    stale_response.status)
        refresh["stale_response_code"] = origin_response["code"]
        refresh["old_draft_preserved_after_rejection"] = (
            page.evaluate("key => sessionStorage.getItem(key)", draft_key) == old_draft_bytes)
        receipt["phases"].append({"phase": "old_fingerprint_continue", **refresh})
        assert stale_response.status == 409
        assert refresh["stale_response_code"] == "DYNAMIC_FIELDS_STALE"
        expect(connected).to_contain_text(
            "The application form changed after you reviewed it. "
            "Reload current fields and review the answers again.")
        expect(form).to_have_count(0)
        assert refresh["old_draft_preserved_after_rejection"] is True
        new_draft_key = draft_key.rsplit(":", 1)[0] + ":" + refreshed_metadata["fingerprint"]
        assert new_draft_key != draft_key
        assert page.evaluate("key => sessionStorage.getItem(key)", new_draft_key) is None

        connected.get_by_role("button", name="Reload changed form fields", exact=True).click()
        expect(form).to_be_visible()
        preference = form.get_by_role("group", name="Additional preference", exact=True).get_by_label(
            "Additional preference", exact=True)
        preference_review = form.get_by_label(
            "Review this answer for Additional preference", exact=True)
        remember("reloaded_definition_requires_fresh_reviews")
        expect(preference).to_be_visible()
        expect(department).to_have_value("")
        expect(department_review).not_to_be_checked()
        expect(row).to_have_count(0)
        expect(row_review).to_have_count(0)
        expect(preference).to_have_value("")
        expect(preference_review).not_to_be_checked()
        expect(continue_button).to_be_disabled()
        department.select_option("Engineering")
        department_review.check()
        team_group.get_by_role("button", name="Add Team", exact=True).click()
        row.select_option("Platform")
        row_review.check()
        remember("new_required_field_still_unanswered")
        expect(preference).to_have_value("")
        expect(preference_review).not_to_be_checked()
        expect(continue_button).to_be_disabled()
        preference.select_option("Daytime")
        remember("new_required_field_answered_but_unreviewed")
        expect(preference_review).not_to_be_checked()
        expect(continue_button).to_be_disabled()
        preference_review.check()
        remember("new_definition_freshly_reviewed")
        expect(continue_button).to_be_enabled()
        with page.expect_response(lambda response: (
                urlsplit(response.url).netloc == origin_netloc
                and urlsplit(response.url).path == fields_path
                and parse_qs(urlsplit(response.url).query).get("expected_fingerprint")
                == [refreshed_metadata["fingerprint"]])) as accepted:
            continue_button.click()
        refresh["fresh_response_status"] = accepted.value.status
        assert refresh["fresh_response_status"] == 200
        expect(connected).to_contain_text(
            "Reviewed answers are saved in this browser tab for this application.")
        refreshed_saved = json.loads(page.evaluate(
            "key => sessionStorage.getItem(key)", new_draft_key))
        refresh["saved_answers"] = refreshed_saved["answers"]
        refresh["saved_fingerprint"] = refreshed_saved["fingerprint"]
        refresh["old_draft_preserved_after_fresh_save"] = (
            page.evaluate("key => sessionStorage.getItem(key)", draft_key) == old_draft_bytes)
        remember("new_fingerprint_draft_saved")
        assert refreshed_saved["answers"] == {
            "department": "Engineering", "team": ["Platform"], "followup": "Daytime"}
        assert refreshed_saved["fingerprint"] == refreshed_metadata["fingerprint"]
        assert "reviewed_fields" not in refreshed_saved
        assert refresh["old_draft_preserved_after_fresh_save"] is True
        final_application = store.application(application_id)
        with store.engine.connect() as connection:
            final_attempts = list(connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == application_id)).mappings())
        refresh["application_unchanged_after_fixture"] = (
            final_application == application_after_fixture)
        refresh["attempts_unchanged_after_fixture"] = final_attempts == attempts_after_fixture
        refresh["final_attempt_count"] = len(final_attempts)
        assert final_application == application_after_fixture
        assert final_attempts == attempts_after_fixture
        assert not outside, outside
        assert not page_errors, page_errors
        assert all(method == "GET" for method, _ in requests), requests
        assert len(stale_origin_responses) == 1
        receipt["outcome"] = "PASS"
    except BaseException as error:
        primary_error = error
        receipt["outcome"] = "FAILURE"
        receipt["failure_class"] = type(error).__name__
        raise
    finally:
        receipt["requests"] = [{"method": method, "path": path} for method, path in requests]
        receipt["blocked_requests"] = outside
        receipt["page_error_count"] = len(page_errors)
        # Retain the first observed behavior before browser/server cleanup.
        try:
            record_property("dynamic_repeatable_select_receipt", json.dumps(receipt, sort_keys=True))
        except Exception as error:
            cleanup["errors"].append("receipt:" + type(error).__name__)
        for name, resource, action in (
                ("context_closed", context, lambda: context.close()),
                ("browser_closed", browser, lambda: browser.close()),
                ("playwright_stopped", playwright, lambda: playwright.stop())):
            if resource is not None:
                try:
                    action()
                    cleanup[name] = True
                except Exception as error:
                    cleanup["errors"].append(name + ":" + type(error).__name__)
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.ident is not None:
            try:
                thread.join(timeout=10)
            except Exception as error:
                cleanup["errors"].append("server_join:" + type(error).__name__)
        cleanup["server_stopped"] = thread is None or not thread.is_alive()
        if cleanup["server_stopped"]:
            if sock is not None:
                try:
                    sock.close()
                except Exception as error:
                    cleanup["errors"].append("socket_close:" + type(error).__name__)
            try:
                store.engine.dispose()
                cleanup["store_disposed"] = True
            except Exception as error:
                cleanup["errors"].append("store_dispose:" + type(error).__name__)
        else:
            cleanup["errors"].append("server_thread_still_alive")
        try:
            record_property("dynamic_repeatable_select_cleanup", json.dumps(cleanup, sort_keys=True))
        except Exception as error:
            cleanup["errors"].append("cleanup_receipt:" + type(error).__name__)
        if cleanup["errors"] and primary_error is None:
            raise AssertionError("Owned repeated-select fixture cleanup failed: "
                                 + ", ".join(cleanup["errors"]))


if __name__ == "__main__":
    unittest.main()
