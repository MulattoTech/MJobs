"""Delayed application readback preserves local edits only within their scope."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.tracking.applications import set_application_pipeline_status
from tests.integration.test_spine import PROFILE


class ApplicationEditDraftTests(unittest.TestCase):
    def test_same_application_read_preserves_edits_and_switches_clear_them(self):
        self.exercise(stale_read=False)

    def test_old_application_read_cannot_replace_new_application_edits(self):
        self.exercise(stale_read=True)

    def exercise(self, *, stale_read):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-drafts-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'drafts.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            other_profile = {**deepcopy(PROFILE), "id": "other", "name": "Other fixture"}
            store.save_profile(other_profile)
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
                discover(store, fixture, "other")
            jobs = store.jobs("fixture")
            first = prepare(store, jobs[0]["id"], "fixture", root)
            second = prepare(store, jobs[1]["id"], "fixture", root)
            other = prepare(store, store.jobs("other")[0]["id"], "other", root)
            with store.engine.begin() as connection:
                for application_id in (second, other):
                    connection.execute(update(m.applications).where(
                        m.applications.c.id == application_id).values(trace=[f"DETAIL_{application_id}"]))
            app = create_app(store, "http://127.0.0.1:1", root, operation_mode="FIXTURE")
            entered, release, armed = threading.Event(), threading.Event(), threading.Event()
            detail_path = f"/profiles/fixture/applications/{first}"

            @app.middleware("http")
            async def delayed_detail(request, call_next):
                if request.method == "GET" and request.url.path == detail_path and armed.is_set():
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(0.01)
                return await call_next(request)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Owned fixture API failed to start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script(
                        "localStorage.setItem('mjobs-profile','fixture');"
                        f"localStorage.setItem('mjobs-selected-application:fixture','{first}');"
                        f"localStorage.setItem('mjobs-selected-application:other','{other}');"
                    )
                    external, writes = [], []

                    def local_only(route):
                        request = route.request
                        if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                            external.append(request.url)
                            route.abort()
                        else:
                            if request.method not in ("GET", "HEAD"):
                                writes.append((request.method, urlsplit(request.url).path))
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin)
                    nav = page.get_by_role("navigation", name="Main navigation")
                    nav.get_by_role("button", name="Applications", exact=False).click()
                    # The trace marker proves the restored detail has committed,
                    # rather than merely observing a completed network request.
                    initial_marker = "SYNTHETIC_INITIAL_DETAIL"
                    with store.engine.begin() as connection:
                        connection.execute(update(m.applications).where(
                            m.applications.c.id == first).values(trace=[initial_marker]))
                    first_card = page.locator(".job-card").filter(has_text=jobs[0]["title"])
                    first_card.click()
                    expect(page.get_by_text(initial_marker, exact=False)).to_be_visible()
                    armed.set()
                    first_card.click()
                    self.assertTrue(entered.wait(3), "Actual second detail read must be held")
                    if stale_read:
                        page.locator(".job-card").filter(has_text=jobs[1]["title"]).click()
                        expect(page.get_by_text(f"DETAIL_{second}", exact=False)).to_be_visible()
                    page.get_by_role("button", name="Add question", exact=True).click()
                    question = "How do you recover a synthetic service?"
                    answer = "I reviewed this fictional service recovery answer."
                    note = "Synthetic unsaved note about on-call rotation."
                    question_field = page.get_by_label("Employer question")
                    answer_field = page.get_by_role("textbox", name="Answer", exact=True)
                    note_field = page.get_by_role("textbox", name="Application note", exact=True)
                    status_field = page.get_by_role("combobox", name="Manual application status", exact=True)
                    question_field.fill(question)
                    answer_field.fill(answer)
                    page.get_by_label("I reviewed this exact answer for this job").check()
                    note_field.fill(note)
                    status_field.select_option(label="Saved")
                    marker = "SYNTHETIC_DELAYED_DETAIL_COMMITTED"
                    if stale_read:
                        page.evaluate("""marker => {
                            window.staleDraftReadRendered = false;
                            window.draftReadObserver = new MutationObserver(() => {
                                if (document.body.textContent.includes(marker))
                                    window.staleDraftReadRendered = true;
                            });
                            window.draftReadObserver.observe(document.body,
                                {subtree:true, childList:true, characterData:true});
                        }""", marker)
                    with store.engine.begin() as connection:
                        connection.execute(update(m.applications).where(
                            m.applications.c.id == first).values(trace=[marker]))
                    with page.expect_response(lambda response: urlsplit(response.url).path == detail_path) as response:
                        release.set()
                    self.assertEqual(response.value.status, 200)
                    if stale_read:
                        # A fresh same-scope read after the old response supplies
                        # an affirmative render boundary for the new application.
                        fresh_marker = "SYNTHETIC_NEW_APPLICATION_REFRESH"
                        with store.engine.begin() as connection:
                            connection.execute(update(m.applications).where(
                                m.applications.c.id == second).values(trace=[fresh_marker]))
                        page.locator(".job-card").filter(has_text=jobs[1]["title"]).click()
                        expect(page.get_by_text(fresh_marker, exact=False)).to_be_visible()
                        expect(page.get_by_text(marker, exact=False)).to_have_count(0)
                        self.assertFalse(page.evaluate("window.staleDraftReadRendered"),
                                         "Rejected detail must never render over the new selection")
                        page.evaluate("window.draftReadObserver.disconnect()")
                    else:
                        expect(page.get_by_text(marker, exact=False)).to_be_visible()
                    expect(question_field).to_have_value(question)
                    expect(answer_field).to_have_value(answer)
                    expect(page.get_by_label("I reviewed this exact answer for this job")).to_be_checked()
                    expect(note_field).to_have_value(note)
                    expect(status_field).to_have_value("SAVED")
                    self.assertEqual(writes, [], "Detail refresh must not persist unreviewed drafts")
                    # Explicit scope switches must clear private drafts, even
                    # when the application was previously visited this session.
                    armed.clear()
                    target = jobs[0] if stale_read else jobs[1]
                    target_id = first if stale_read else second
                    page.locator(".job-card").filter(has_text=target["title"]).click()
                    expect(page.get_by_text(marker if stale_read else f"DETAIL_{target_id}", exact=False)).to_be_visible()
                    expect(question_field).to_have_count(0)
                    expect(note_field).to_have_value("")
                    expect(status_field).to_have_value("")
                    # An untouched status still follows durable server updates.
                    set_application_pipeline_status(store, "fixture", target_id,
                                                    "READY", "synthetic-pristine-status")
                    status_marker = "SYNTHETIC_TRACKING_REFRESH"
                    with store.engine.begin() as connection:
                        connection.execute(update(m.applications).where(
                            m.applications.c.id == target_id).values(trace=[status_marker]))
                    page.locator(".job-card").filter(has_text=target["title"]).click()
                    expect(page.get_by_text(status_marker, exact=False)).to_be_visible()
                    expect(status_field).to_have_value("READY")
                    page.get_by_role("button", name="Add question", exact=True).click()
                    question_field.fill("Private draft on previous profile")
                    note_field.fill("Private previous profile note")
                    status_field.select_option(label="Saved")
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    expect(page.get_by_text(f"DETAIL_{other}", exact=False)).to_be_visible()
                    expect(question_field).to_have_count(0)
                    expect(note_field).to_have_value("")
                    expect(status_field).to_have_value("")
                    self.assertEqual(external, [])
                    self.assertEqual(writes, [])
                    context.close()
                    browser.close()
            finally:
                release.set()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
