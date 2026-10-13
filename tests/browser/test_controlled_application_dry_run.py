"""Mounted SPA → reviewed local fixture → worker → receipt after reload."""
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import update

from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from tests.integration import test_controlled_application_dry_run as flow


class ControlledApplicationDryRunBrowserTests(unittest.TestCase):
    _new_store = flow.ControlledApplicationDryRunTests._new_store
    setUp = flow.ControlledApplicationDryRunTests.setUp
    tearDown = flow.ControlledApplicationDryRunTests.tearDown
    execute = flow.ControlledApplicationDryRunTests.execute

    def test_review_prepare_and_read_verified_receipt_after_reload_for_both_providers(self):
        uploaded=self.client.post("/profiles/fixture/document-library",content=b"Synthetic browser supporting evidence",
            headers={"Content-Type":"text/plain","X-Filename":"fixture-evidence.txt",
                     "X-Document-Kind":"SUPPORTING_DOCUMENT","X-Document-Label":"Fixture evidence"})
        self.assertEqual(uploaded.status_code,201,uploaded.text)
        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(self.client.app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
        thread.start()
        deadline=time.monotonic()+10
        external, errors, writes = [], [], []
        application = self.store.application(self.application_id)
        job = self.store.job(application["job_id"],profile_id="fixture")
        try:
            while not server.started:
                if not thread.is_alive() or time.monotonic()>deadline:
                    self.fail("Isolated API did not start")
                time.sleep(.02)
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True)
                context=browser.new_context()
                context.add_init_script("localStorage.setItem('mjobs-profile','fixture')")
                def local_only(route):
                    if urlsplit(route.request.url).netloc!=urlsplit(origin).netloc:
                        external.append(urlsplit(route.request.url).netloc)
                        route.abort()
                    else:route.continue_()
                context.route("**/*",local_only)
                page=context.new_page()
                page.set_default_timeout(8000)
                page.on("pageerror",lambda error:errors.append(str(error)))
                def record(request):
                    if request.method not in {"GET","HEAD","OPTIONS"}:
                        writes.append((request.method,urlsplit(request.url).path,request.post_data or ""))
                page.on("request",record)
                def open_application():
                    page.goto(origin,wait_until="domcontentloaded")
                    page.get_by_role("button",name="Applications",exact=False).click()
                    page.get_by_role("region",name="Application pipeline").get_by_role("button",name=job["title"],exact=False).click()
                    return page.get_by_role("region",name="Controlled ATS preparation",exact=True)
                for provider,url in (
                    ("greenhouse","https://boards.greenhouse.io/example/jobs/12345"),
                    ("lever","https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef"),
                ):
                    with self.subTest(provider=provider):
                        with self.store.engine.begin() as connection:
                            connection.execute(update(m.routes).where(m.routes.c.id==application["route_id"])
                                               .values(provider=provider,url=url))
                        panel=open_application()
                        expect(panel.get_by_label("email fixture value",exact=True)).to_have_text("alex@example.test")
                        expect(panel.get_by_label("weekend_availability fixture value",exact=True)).to_have_text("Yes, weekends are available.")
                        expect(panel.get_by_role("button",name="Run controlled fixture",exact=True)).to_be_disabled()
                        if provider=="greenhouse":
                            panel.get_by_role("checkbox",name="Fixture evidence",exact=True).check()
                            expect(panel.get_by_role("checkbox",name="I reviewed these fixture values, selected documents, and the ordered page plan",exact=True)).to_be_disabled()
                            panel.get_by_role("button",name="Save fixture document selection",exact=True).click()
                            expect(panel.get_by_text("Fixture document selection saved. Review the updated values before running.",exact=True)).to_be_visible()
                        panel.get_by_role("checkbox",name="I reviewed these fixture values, selected documents, and the ordered page plan",exact=True).check()
                        if provider=="greenhouse":
                            def lose_review_reply(route):
                                response=route.fetch()
                                self.assertEqual(response.status,200)
                                route.abort("failed")
                            context.route("**/controlled-dry-run/reviews",lose_review_reply,times=1)
                            panel.get_by_role("button",name="Record fixture review",exact=True).click()
                            expect(panel.get_by_text("The request could not be confirmed. Retry to recover the same request, or refresh its saved result.",exact=True)).to_be_visible()
                        panel.get_by_role("button",name="Record fixture review",exact=True).click()
                        expect(panel.get_by_text("Fixture review saved.",exact=True)).to_be_visible()
                        with page.expect_response(lambda response: response.request.method=="POST" and response.url.endswith("/controlled-dry-run/runs")) as admission:
                            panel.get_by_role("button",name="Run controlled fixture",exact=True).click()
                        queued=admission.value.json()
                        self.assertEqual(admission.value.status,202,queued)
                        # The product worker has its own event loop/process;
                        # do not nest its sync Playwright loop in the UI driver.
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            result=executor.submit(self.execute,queued["operation_id"]).result(timeout=60)
                        self.assertEqual(result["status"],"COMPLETED",result)
                        expect(panel.get_by_text("Fixture run: COMPLETED",exact=True)).to_be_visible()
                        expect(panel.get_by_role("region",name="Saved fixture verification")).to_be_visible()
                        panel=open_application()
                        expect(panel.get_by_text("Fixture run: COMPLETED",exact=True)).to_be_visible()
                        expect(panel.get_by_text("Selected document bytes verified locally. No employer acceptance or submission is claimed.",exact=True)).to_be_visible()
                        self.assertEqual(self.store.count("application_attempts"),0)
                browser.close()
            self.assertEqual(external,[])
            self.assertEqual(errors,[])
            self.assertEqual(len(writes),6,writes)
            review_bodies=[body for _,path,body in writes if path.endswith("/controlled-dry-run/reviews")]
            self.assertEqual(review_bodies[0],review_bodies[1],"Lost review response must retain request identity")
            for method,path,body in writes:
                self.assertEqual(method,"PUT" if path.endswith("/controlled-dry-run/supporting-documents") else "POST")
                self.assertTrue(path.endswith(("/controlled-dry-run/reviews","/controlled-dry-run/runs","/controlled-dry-run/supporting-documents")),path)
                self.assertNotIn("alex@example.test",body)
                self.assertNotIn("Yes, weekends are available.",body)
                self.assertNotIn("submit",json.loads(body))
        finally:
            server.should_exit=True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive(),"Owned synthetic API did not stop")
