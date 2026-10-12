"""Built UI -> scoped API -> durable outbox -> real Chromium mock worker acceptance."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import sync_playwright, expect
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import read_attempt_history
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before connected acceptance")
class MockRunBrowserHarness(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="mjobs-run-controls-"))
        self.root = Path(directory)
        self.fixture = self.enterContext(fixture_server())
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate(); self.store.bind_workspace_mode("FIXTURE"); self.store.save_profile(dict(PROFILE))
        discover(self.store, self.fixture, "fixture")
        self.job = self.store.jobs("fixture")[0]
        self.app_id = prepare(self.store, self.job["id"], "fixture", self.root)
        app = create_app(self.store, self.fixture, self.root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        self.socket = bound_browser_socket()
        self.origin = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets":[self.socket]}, daemon=True)
        self.thread.start(); self.addCleanup(self.stop_server)
        deadline = time.monotonic()+10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic()>deadline:
                self.fail("Isolated run-controls API did not start")
            time.sleep(0.02)
        playwright = self.enterContext(sync_playwright())
        browser = playwright.chromium.launch(headless=True)
        self.addCleanup(browser.close)
        self.context = browser.new_context(); self.addCleanup(self.context.close)
        self.external = []
        def local_only(route):
            if urlsplit(route.request.url).netloc != urlsplit(self.origin).netloc:
                self.external.append(route.request.url); route.abort()
            else:
                route.continue_()
        self.context.route("**/*", local_only)
        self.page = self.context.new_page(); self.page.set_default_timeout(12000)
        self.posts = []
        def track(request):
            if request.method == "POST" and urlsplit(request.url).path.endswith("/mock-run"):
                self.posts.append(request.post_data_json)
        self.page.on("request", track)
        self.page.goto(self.origin, wait_until="domcontentloaded")
        self.page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
        self.open_application()

    def stop_server(self):
        self.server.should_exit=True; self.thread.join(timeout=10); self.socket.close()
        self.assertFalse(self.thread.is_alive(), "Test-owned API did not stop")

    def open_application(self, run_controls=True):
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Applications", exact=False).click()
        self.page.get_by_role("region", name="Application pipeline").get_by_role(
            "button", name=self.job["title"], exact=False).click()
        self.panel = self.page.get_by_role("region", name="Recoverable mock run", exact=True)
        if run_controls:
            expect(self.panel).to_be_visible()

    def operations(self):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.operations).where(
                m.operations.c.kind == "APPLY_MOCK", m.operations.c.entity_id == self.app_id)).mappings()]

    def counts(self):
        return httpx.get(self.fixture+"/_test/request-counts", trust_env=False).json()

    def run_worker(self, operation_id, expected_status="COMPLETED", refresh_panel=True):
        env={**os.environ, "MJOBS_DATABASE_URL":self.store.url, "MJOBS_OUTPUT_DIR":str(self.root),
             "MJOBS_MOCK_ORIGIN":self.fixture, "MJOBS_OPERATION_MODE":"FIXTURE", "PYTHONUTF8":"1"}
        code="import json,sys;from workers.tasks import execute_operation;print(json.dumps(execute_operation(sys.argv[1])))"
        child=subprocess.run([sys.executable,"-c",code,operation_id],cwd=ROOT,env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(child.returncode,0,child.stderr[-2000:])
        result=json.loads(child.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"],expected_status,result)
        if not refresh_panel:
            return result
        expect(self.panel.get_by_role("button",name="Refresh run status",exact=True)).to_be_enabled()
        self.panel.get_by_role("button",name="Refresh run status",exact=True).click()
        expect(self.panel).to_contain_text("COMPLETED")



class MockRunControlsBrowserTests(MockRunBrowserHarness):
    def test_late_terminal_refresh_does_not_navigate_away_from_profile(self):
        button = self.panel.get_by_role("button", name="Run dry-run", exact=True)
        expect(button).to_be_enabled(); button.click()
        expect(self.panel).to_contain_text("QUEUED")
        held = []
        def delay_application(route):
            held.append((route, route.fetch()))
        self.page.route(f"{self.origin}/profiles/fixture/applications/{self.app_id}", delay_application)
        self.run_worker(self.operations()[0]["id"], refresh_panel=False)
        expect(self.panel).to_contain_text("COMPLETED")
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            self.page.wait_for_timeout(20)
        self.assertEqual(len(held), 1)
        self.page.get_by_role("navigation").get_by_role("button", name="Profile", exact=True).click()
        library = self.page.get_by_role("region", name="Document library", exact=True)
        expect(library).to_be_visible()
        with self.page.expect_response(lambda response: urlsplit(response.url).path == "/profiles"):
            held[0][0].fulfill(response=held[0][1])
        expect(library).to_be_visible()
        self.assertEqual(self.counts(), {"review": 1, "submit": 0})
        self.assertFalse(self.external)

    def test_lost_acknowledgement_reload_and_explicit_new_intent_are_distinct(self):
        committed=[]
        def lose_acknowledgement(route):
            response=route.fetch()
            self.assertEqual(response.status,202)
            committed.append(response.json())
            route.abort("failed")
        endpoint=f"{self.origin}/profiles/fixture/applications/{self.app_id}/mock-run"
        self.page.route(endpoint,lose_acknowledgement,times=1)
        button=self.panel.get_by_role("button",name="Run dry-run",exact=True)
        expect(button).to_be_enabled(); button.click()
        expect(self.panel.get_by_role("alert")).to_be_visible()
        self.assertEqual(len(committed),1)
        self.assertEqual(len(self.operations()),1)
        request_id=self.posts[0]["request_id"]
        expect(self.panel).to_contain_text(request_id)
        expect(self.panel.get_by_role("button",name="Resend same request",exact=True)).to_be_visible()
        self.open_application()
        expect(self.panel).to_contain_text("QUEUED")
        expect(self.panel).to_contain_text(request_id)
        self.assertEqual(len(self.posts),1, "Reload sent another POST")
        first=committed[0]["operation_id"]
        self.run_worker(first)
        expect(self.page.get_by_role("status",name="Application state",exact=True)).to_have_text("DRY_RUN_COMPLETE")
        self.assertEqual(self.counts(),{"review":1,"submit":0})
        self.assertEqual(len(read_attempt_history(self.store,"fixture",self.app_id)["attempts"]),1)
        next_button=self.panel.get_by_role("button",name="Start a new reviewed run",exact=True)
        expect(next_button).to_be_enabled(); next_button.click()
        run=self.panel.get_by_role("button",name="Run dry-run",exact=True)
        expect(run).to_be_enabled()
        run.evaluate("button=>{button.click();button.click();}")
        expect(self.panel).to_contain_text("QUEUED")
        self.assertEqual(len(self.posts),2, "Rapid clicks admitted more than one new intent")
        self.assertNotEqual(self.posts[1]["request_id"],request_id)
        operations=self.operations(); self.assertEqual(len(operations),2)
        second=next(row["id"] for row in operations if row["id"]!=first)
        self.run_worker(second)
        self.assertEqual(self.counts(),{"review":2,"submit":0})
        self.assertEqual(len(read_attempt_history(self.store,"fixture",self.app_id)["attempts"]),2)
        self.assertFalse(self.external)

    def test_queued_cancel_is_visible_and_survives_reload_without_work(self):
        run=self.panel.get_by_role("button",name="Run dry-run",exact=True)
        expect(run).to_be_enabled();run.click()
        expect(self.panel).to_contain_text("QUEUED")
        cancel=self.panel.get_by_role("button",name="Cancel queued or running test",exact=True)
        expect(cancel).to_be_enabled();cancel.click()
        expect(self.panel).to_contain_text("CANCELLED")
        request_id=self.posts[0]["request_id"]
        self.open_application()
        expect(self.panel).to_contain_text("CANCELLED")
        expect(self.panel).to_contain_text(request_id)
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.operations()[0]["state"],"CANCELLED")
        self.assertEqual(self.store.count("application_attempts"),0)
        self.assertEqual(self.counts(),{"review":0,"submit":0})
        self.assertFalse(self.external)

    def test_explicit_fictional_submission_consent_and_reload_never_duplicate_submit(self):
        submit=self.panel.get_by_role("button",name="Submit to local mock",exact=True)
        expect(submit).to_be_disabled()
        consent=self.panel.get_by_role("checkbox",name="I approve one fictional mock submission",exact=True)
        expect(consent).to_be_enabled();consent.check();submit.click()
        expect(self.panel).to_contain_text("QUEUED")
        self.assertTrue(self.posts[0]["submit"])
        self.open_application()
        expect(self.panel).to_contain_text("QUEUED")
        self.assertEqual(len(self.posts),1)
        self.run_worker(self.operations()[0]["id"])
        self.assertEqual(self.store.application(self.app_id)["state"],"CONFIRMED")
        expect(self.page.get_by_role("status",name="Application state",exact=True)).to_have_text("CONFIRMED")
        self.open_application()
        expect(self.panel).to_contain_text("COMPLETED")
        expect(self.panel.get_by_role("button",name="Start a new reviewed run",exact=True)).to_be_disabled()
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.counts(),{"review":1,"submit":1})
        self.assertEqual(len(read_attempt_history(self.store,"fixture",self.app_id)["attempts"]),1)
        self.assertFalse(self.external)


if __name__ == "__main__":
    unittest.main()
