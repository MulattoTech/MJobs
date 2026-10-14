"""Connected Activity UI preserves safe historical subjects across reload."""
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
from services.audit_history import record_job_merged
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


class AuditSubjectHistoryBrowserTests(unittest.TestCase):
    def test_activity_keeps_historical_subject_after_rename_and_profile_switch(self):
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
                    discovered = history.get_by_role("listitem").filter(has_text="JOB_DISCOVERED")
                    expect(discovered.first).to_be_visible()
                    initial = discovered.all_text_contents()
                    initial_api = page.request.get(origin + "/profiles/fixture/audit-history").json()
                    with store.engine.begin() as connection:
                        connection.execute(update(m.jobs).values(title="Renamed current job"))
                        connection.execute(update(m.companies).values(name="Renamed current employer"))
                    page.reload()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    discovered = history.get_by_role("listitem").filter(has_text="JOB_DISCOVERED")
                    expect(discovered.first).to_be_visible()
                    self.assertEqual(discovered.all_text_contents(), initial)
                    # A subsequent real event must snapshot safely at write time.
                    with store.engine.begin() as connection:
                        connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                            title="Engineer api_key=SYNTHETIC_PRIVATE_ACTIVITY"))
                        record_job_merged(connection, "fixture", job_id, "browser-private-posting")
                    page.reload()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    merged = history.get_by_role("listitem").filter(has_text="JOB_MERGED")
                    expect(merged).to_contain_text("Details withheld")
                    expect(history).not_to_contain_text("SYNTHETIC_PRIVATE")
                    response = page.request.get(origin + "/profiles/fixture/audit-history")
                    self.assertNotIn("SYNTHETIC_PRIVATE", response.text())
                    before = {e["id"]: e for e in initial_api["events"] if e["type"] == "JOB_DISCOVERED"}
                    after = {e["id"]: e for e in response.json()["events"] if e["type"] == "JOB_DISCOVERED"}
                    self.assertEqual(after, before)
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
