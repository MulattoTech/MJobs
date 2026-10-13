"""Mounted SPA review and real worker proof for the ordered local fixture pages."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update

from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from tests.integration import test_controlled_application_dry_run as flow


class ControlledMultipageDryRunBrowserTests(unittest.TestCase):
    _new_store = flow.ControlledApplicationDryRunTests._new_store
    setUp = flow.ControlledApplicationDryRunTests.setUp
    tearDown = flow.ControlledApplicationDryRunTests.tearDown
    execute = flow.ControlledApplicationDryRunTests.execute

    def test_ordered_pages_are_reviewed_then_worker_proof_and_documents_survive_reload(self):
        uploaded = self.client.post(
            "/profiles/fixture/document-library",
            content=b"Synthetic multipage browser supporting evidence",
            headers={
                "Content-Type": "text/plain",
                "X-Filename": "fixture-evidence.txt",
                "X-Document-Kind": "SUPPORTING_DOCUMENT",
                "X-Document-Label": "Fixture evidence",
            },
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)

        application = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(
                update(m.routes)
                .where(m.routes.c.id == application["route_id"])
                .values(
                    provider="greenhouse",
                    url="https://boards.greenhouse.io/example/jobs/12345",
                )
            )

        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(self.client.app, log_level="error", access_log=False)
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 10
        external, errors, writes = [], [], []
        job = self.store.job(application["job_id"], profile_id="fixture")
        try:
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    self.fail("Isolated API did not start")
                time.sleep(0.02)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context()
                    context.add_init_script(
                        "localStorage.setItem('mjobs-profile','fixture')"
                    )

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(urlsplit(route.request.url).netloc)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def record(request):
                        if request.method not in {"GET", "HEAD", "OPTIONS"}:
                            writes.append(
                                (request.method, urlsplit(request.url).path, request.post_data or "")
                            )

                    page.on("request", record)

                    def open_panel():
                        page.goto(origin, wait_until="domcontentloaded")
                        page.get_by_role("button", name="Applications", exact=False).click()
                        page.get_by_role("region", name="Application pipeline").get_by_role(
                            "button", name=job["title"], exact=False
                        ).click()
                        return page.get_by_role(
                            "region", name="Controlled ATS preparation", exact=True
                        )

                    panel = open_panel()
                    plan = panel.get_by_role("region", name="Ordered fixture page plan", exact=True)
                    entries = plan.locator(":scope > ol > li")
                    expect(entries).to_have_count(2)
                    expect(entries.nth(0)).to_contain_text("Contact and answer")
                    expect(entries.nth(1)).to_contain_text("Documents")

                    # Capture the API contract as well as the rendered order.
                    preview = self.client.get(
                        f"/profiles/fixture/applications/{self.application_id}/controlled-dry-run"
                    )
                    self.assertEqual(preview.status_code, 200, preview.text)
                    pages = preview.json().get("pages")
                    self.assertIsInstance(pages, list, preview.json())
                    self.assertEqual(
                        [(item["id"], item["title"]) for item in pages],
                        [("contact", "Contact and answer"), ("documents", "Documents")],
                    )

                    panel.get_by_role("checkbox", name="Fixture evidence", exact=True).check()
                    panel.get_by_role(
                        "button", name="Save fixture document selection", exact=True
                    ).click()
                    expect(
                        panel.get_by_text(
                            "Fixture document selection saved. Review the updated values before running.",
                            exact=True,
                        )
                    ).to_be_visible()

                    acknowledgement = panel.get_by_role(
                        "checkbox",
                        name="I reviewed these fixture values, selected documents, and the ordered page plan",
                        exact=True,
                    )
                    acknowledgement.check()
                    panel.get_by_role("button", name="Record fixture review", exact=True).click()
                    expect(panel.get_by_text("Fixture review saved.", exact=True)).to_be_visible()

                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith("/controlled-dry-run/runs")
                    ) as admission:
                        panel.get_by_role("button", name="Run controlled fixture", exact=True).click()
                    queued = admission.value.json()
                    self.assertEqual(admission.value.status, 202, queued)

                    # This executes the actual queued operation, whose worker launches
                    # Chromium against the local two-page fixture and checks its controls.
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        result = executor.submit(self.execute, queued["operation_id"]).result(timeout=60)
                    self.assertEqual(result["status"], "COMPLETED", result)

                    receipt_response = self.client.get(
                        f"/profiles/fixture/applications/{self.application_id}/controlled-dry-run/runs/{queued['request_id']}"
                    )
                    self.assertEqual(receipt_response.status_code, 200, receipt_response.text)
                    receipt = receipt_response.json()["receipt"]
                    self.assertEqual(
                        receipt["pages"],
                        [
                            {"id": "contact", "control_count": 3, "validation": "PASS"},
                            {"id": "documents", "control_count": 3, "validation": "PASS"},
                        ],
                    )
                    self.assertEqual(len(receipt["documents"]), 3)
                    expect(panel.get_by_text("Fixture run: COMPLETED", exact=True)).to_be_visible()
                    proof = panel.get_by_role("region", name="Saved fixture page proof", exact=True)
                    proof_items = proof.locator(":scope > ol > li")
                    expect(proof_items).to_have_count(2)
                    expect(proof_items.nth(0)).to_contain_text("Contact and answer · 3 controls · PASS")
                    expect(proof_items.nth(1)).to_contain_text("Documents · 3 controls · PASS")

                    page.reload(wait_until="domcontentloaded")
                    panel = open_panel()
                    proof = panel.get_by_role("region", name="Saved fixture page proof", exact=True)
                    proof_items = proof.locator(":scope > ol > li")
                    expect(proof_items).to_have_count(2)
                    expect(proof_items.nth(0)).to_contain_text("Contact and answer · 3 controls · PASS")
                    expect(proof_items.nth(1)).to_contain_text("Documents · 3 controls · PASS")
                    expect(
                        panel.get_by_text(
                            "Selected document bytes verified locally. No employer acceptance or submission is claimed.",
                            exact=True,
                        )
                    ).to_be_visible()
                    self.assertEqual(self.store.count("application_attempts"), 0)
                finally:
                    browser.close()

            self.assertEqual(external, [])
            self.assertEqual(errors, [])
            self.assertTrue(writes)
            self.assertFalse(
                any("submit" in path.lower() or path.startswith("/ats/") for _, path, _ in writes),
                writes,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive(), "Owned synthetic API did not stop")


if __name__ == "__main__":
    unittest.main()
