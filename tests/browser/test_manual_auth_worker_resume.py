"""Manual fixture sign-in must resume actual worker execution, not a stale outbox row."""
import os
import subprocess
import sys
from unittest.mock import patch

from playwright.sync_api import expect
from sqlalchemy import select, update

from packages.persistence import models as m
from services.applications.attempt_history import read_attempt_history
from tests.browser.test_mock_run_controls import MockRunBrowserHarness, ROOT


class ManualAuthWorkerResumeTests(MockRunBrowserHarness):
    def pause_legacy_worker_and_sign_in(self):
        application=self.store.application(self.app_id)
        with self.store.engine.begin() as connection:
            route=dict(connection.execute(select(m.routes).where(m.routes.c.id==application["route_id"])).mappings().one())
            connection.execute(update(m.routes).where(m.routes.c.id==route["id"]).values(
                url=f"{self.fixture}/ats/apply/{route['requisition']}?auth_expired=true"))
        response=self.context.request.post(f"{self.origin}/profiles/fixture/applications/{self.app_id}/mock-run",data={"submit":False})
        self.assertEqual(response.status,202)
        legacy=response.json()["operation_id"]
        self.run_worker(legacy,expected_status="BLOCKED",refresh_panel=False)
        self.assertEqual(self.store.application(self.app_id)["state"],"AUTH_REQUIRED")
        before=read_attempt_history(self.store,"fixture",self.app_id)["attempts"][0]
        self.assertEqual(before["state"],"AUTH_REQUIRED")
        self.open_application(run_controls=False)
        panel=self.page.get_by_role("region",name="Manual MOCK authentication",exact=True)
        expect(panel).to_be_visible()
        self.assertEqual(self.posts,[])

        def human_mock_window(*args,**kwargs):
            self.assertEqual(kwargs["application_id"],self.app_id)
            self.assertEqual(kwargs["mock_origin"],self.fixture)
            # Substitute only the interactive headed window with a test-owned,
            # headless browser performing normal login on the fictional provider.
            # No cookie is read, fabricated, returned to the UI, or logged.
            code='''import sys
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    context=p.chromium.launch_persistent_context(sys.argv[1],headless=True,args=["--disable-gpu"])
    allowed=urlsplit(sys.argv[2]).netloc
    context.route("**/*",lambda route:route.continue_() if urlsplit(route.request.url).netloc==allowed else route.abort())
    page=context.pages[0] if context.pages else context.new_page()
    page.goto(sys.argv[2],wait_until="domcontentloaded")
    page.get_by_label("Fixture username",exact=True).fill("synthetic-fixture-user")
    page.get_by_label("Fixture password",exact=True).fill("synthetic-fixture-only-password")
    page.get_by_role("button",name="Sign in",exact=True).click()
    page.wait_for_url("**/ats/apply/*?auth_expired=true")
    assert page.locator("[data-auth-required]").count()==0
    context.close()
'''
            profile=(self.root/self.app_id/"browser-profile").resolve()
            self.assertTrue(profile.is_relative_to(self.root.resolve()))
            login=f"{self.fixture}/ats/login/{route['requisition']}"
            child=subprocess.run([sys.executable,"-c",code,str(profile),login],cwd=ROOT,
                                 env={**os.environ,"PYTHONUTF8":"1"},capture_output=True,text=True,timeout=60)
            self.assertEqual(child.returncode,0,child.stderr[-1500:])
            return "MANUAL_AUTH_WINDOW_CLOSED"

        with patch("apps.api.manual_auth.open_mock_authentication",side_effect=human_mock_window) as opened:
            with self.page.expect_response(lambda response:response.url.endswith("/manual-auth")) as response:
                panel.get_by_role("button",name="Open MOCK sign-in",exact=True).click()
            self.assertEqual(response.value.status,200)
            opened.assert_called_once()
        self.assertEqual(self.store.application(self.app_id)["state"],"AUTH_REQUIRED")
        self.assertEqual(len(self.operations()),1)
        self.assertEqual(self.posts,[])
        self.assertEqual(self.counts(),{"review":0,"submit":0})
        self.assertNotIn("synthetic-fixture-only-password",self.page.locator("body").inner_text())
        button=panel.get_by_role("button",name="Rerun MOCK application",exact=True)
        expect(button).to_be_enabled()
        return legacy,before,button

    def assert_resumed_once(self,legacy,before,new):
        self.assertNotEqual(new,legacy,"Separate manual rerun returned the original terminal AUTH_REQUIRED operation")
        self.run_worker(new,refresh_panel=False)
        self.panel=self.page.get_by_role("region",name="Recoverable mock run",exact=True)
        expect(self.panel).to_be_visible()
        expect(self.panel).to_contain_text("COMPLETED")
        self.assertEqual(self.store.application(self.app_id)["state"],"DRY_RUN_COMPLETE")
        history=read_attempt_history(self.store,"fixture",self.app_id)["attempts"]
        self.assertEqual(len(history),2)
        self.assertEqual(next(row for row in history if row["attempt_id"]==before["attempt_id"]),before)
        self.assertEqual(next(row for row in self.operations() if row["id"]==legacy)["state"],"BLOCKED")
        self.assertEqual(self.counts(),{"review":1,"submit":0})
        self.open_application()
        expect(self.panel).to_contain_text("COMPLETED")
        self.assertEqual(len(self.posts),1)
        self.assertFalse(self.posts[0]["submit"])
        self.assertFalse(self.external)

    def test_separate_manual_sign_in_resumes_worker_in_new_dry_run_not_terminal_legacy_row(self):
        legacy,before,button=self.pause_legacy_worker_and_sign_in()
        with self.page.expect_response(lambda response:response.url.endswith("/mock-run")) as response:
            button.click()
        self.assertEqual(response.value.status,202)
        new=response.value.json()["operation_id"]
        self.assert_resumed_once(legacy,before,new)

    def test_lost_manual_resume_acknowledgement_keeps_pending_request_and_does_not_post_again(self):
        legacy,before,button=self.pause_legacy_worker_and_sign_in()
        admitted=[]
        def lose_response(route):
            response=route.fetch();self.assertEqual(response.status,202)
            admitted.append(response.json());route.abort("failed")
        endpoint=f"{self.origin}/profiles/fixture/applications/{self.app_id}/mock-run"
        self.page.route(endpoint,lose_response,times=1)
        button.click()
        expect(self.page.get_by_role("alert")).to_be_visible()
        expect(button).to_be_enabled()
        self.assertEqual(len(admitted),1)
        self.assertNotEqual(admitted[0]["operation_id"],legacy,"Legacy terminal row was returned instead of new admission")
        with self.page.expect_response(lambda response:"/mock-runs/" in response.url):
            button.click()
        self.assertEqual(len(self.posts),1,"Lost acknowledgement caused a second admission POST")
        self.assert_resumed_once(legacy,before,admitted[0]["operation_id"])
