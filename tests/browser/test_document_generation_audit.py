"""Connected Activity UI retains generation events without artifact/secret leakage."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete, select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.audit_history import record_mock_application_submitted
from services.tracking.applications import add_application_note
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


class DocumentGenerationAuditBrowserTests(unittest.TestCase):
    def test_committed_document_events_survive_removal_reload_and_profile_switch(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-document-audit-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "email": "other@example.invalid"})
            with fixture_server() as fixture_origin:
                discover(store, fixture_origin, "fixture")
            job_id = store.jobs("fixture")[0]["id"]
            application_id = prepare(store, job_id, "fixture", root)
            record_mock_application_submitted(store, application_id)
            store.transition(application_id, ['PREPARED'], 'CONFIRMED')
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                    title="Engineer api_key=SYNTHETIC_PRIVATE_ACTIVITY"))
            add_application_note(store, 'fixture', application_id, 'SYNTHETIC_PRIVATE_NOTE', 'browser-note')
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
                        self.fail("Isolated document-audit API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw, pw.chromium.launch(headless=True) as browser:
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
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    expect(history).to_contain_text("Resume prepared")
                    expect(history).to_contain_text("Cover letter prepared")
                    expect(history).to_contain_text("Fictional mock application outcome confirmed")
                    expect(history).to_contain_text("Details withheld")
                    expect(history).not_to_contain_text("SYNTHETIC_PRIVATE")
                    initial = page.request.get(origin + "/profiles/fixture/audit-history").json()
                    with store.engine.begin() as connection:
                        connection.execute(update(m.jobs).values(title="Later application title"))
                        connection.execute(update(m.companies).values(name="Later application employer"))
                        connection.execute(delete(m.documents).where(
                            m.documents.c.application_id == application_id))
                        connection.execute(delete(m.application_route_choices).where(
                            m.application_route_choices.c.application_id == application_id))
                        connection.execute(delete(m.applications).where(m.applications.c.id == application_id))
                    page.reload()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    expect(history).to_contain_text("Resume prepared")
                    expect(history).to_contain_text("Cover letter prepared")
                    expect(history).to_contain_text("Fictional mock application outcome confirmed")
                    expect(history).not_to_contain_text("Later application")
                    self.assertEqual(page.request.get(origin + "/profiles/fixture/audit-history").json(), initial)
                    # A legacy event keeps its timestamp, without invented kinds.
                    with store.engine.begin() as connection:
                        row = connection.execute(select(m.audit).where(
                            m.audit.c.entity_id == application_id,
                            m.audit.c.action == "DOCUMENTS_PREPARED")).mappings().one()
                        payload = dict(row["payload"])
                        payload.pop("document_kinds")
                        connection.execute(update(m.audit).where(m.audit.c.id == row["id"]).values(payload=payload))
                        connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                            title="Engineer api_key=SYNTHETIC_PRIVATE_ACTIVITY"))
                    page.reload()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    expect(history).to_contain_text("Documents prepared (document types not recorded)")
                    expect(history).to_contain_text("Details withheld")
                    expect(history).not_to_contain_text("SYNTHETIC_PRIVATE")
                    expect(history).not_to_contain_text("Resume prepared")
                    expect(history).not_to_contain_text("Cover letter prepared")
                    response = page.request.get(origin + "/profiles/fixture/audit-history")
                    self.assertNotIn("SYNTHETIC_PRIVATE", response.text())
                    page.get_by_role("button", name="Profile", exact=True).click()
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    other = page.get_by_role("list", name="Audit history")
                    expect(other).to_contain_text("Profile details updated")
                    expect(other).not_to_contain_text("Documents prepared")
                    expect(other).not_to_contain_text("Resume prepared")
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
