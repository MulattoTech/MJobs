"""Connected Chromium acceptance for profile-scoped pipeline preparation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class PipelineWorkbenchBrowserTests(unittest.TestCase):
    def test_pipeline_prepare_queues_and_refreshes_profile_scoped_status(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-pipeline-workbench-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'pipeline-workbench.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            store.save_profile({
                **deepcopy(PROFILE), "id": "other-profile",
                "name": "Other Synthetic Profile", "email": "other@example.test",
            })
            self._seed_profile_job(store, "fixture", "a" * 64,
                                    "Synthetic Pipeline Engineer", "Synthetic Pipeline Employer")
            self._seed_profile_job(store, "other-profile", "b" * 64,
                                    "Foreign Synthetic Role", "Foreign Synthetic Employer")

            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            external_requests = []
            page_errors = []

            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated pipeline workbench API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 1000})
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    response = page.goto(origin, wait_until="domcontentloaded")
                    self.assertIsNotNone(response)
                    self.assertEqual(response.status, 200)
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    page.get_by_role("button", name="Pipeline", exact=True).click()
                    workbench = page.get_by_role("region", name="Pipeline workbench")
                    expect(workbench).to_be_visible()
                    expect(workbench).to_contain_text("Synthetic Pipeline Engineer")
                    expect(workbench).to_contain_text("Synthetic Pipeline Employer")
                    expect(workbench).not_to_contain_text("Foreign Synthetic")
                    prepare_button = workbench.get_by_role(
                        "button", name="Prepare application documents", exact=True,
                    )
                    expect(prepare_button).to_be_enabled()

                    endpoint = "/profiles/fixture/pipeline-workbench/" + "a" * 64 + "/prepare"
                    with page.expect_response(lambda value:
                            value.request.method == "POST" and
                            urlsplit(value.url).path == endpoint) as queued:
                        prepare_button.click()
                    self.assertEqual(queued.value.status, 202, queued.value.text())
                    accepted = queued.value.json()
                    self.assertTrue(accepted["request_id"])
                    self.assertEqual(accepted["job_id"], "a" * 64)
                    self.assertEqual(accepted["state"], "QUEUED")
                    self.assertEqual(accepted["next_action"], "REFRESH_WORKBENCH")
                    self.assertTrue(accepted["operation_id"])
                    self.assertNotIn("private", queued.value.text().casefold())
                    expect(workbench.get_by_text(
                        "Preparation queued. It has not been submitted.", exact=True,
                    )).to_be_visible()

                    with page.expect_response(lambda value:
                            value.request.method == "GET" and
                            urlsplit(value.url).path == "/profiles/fixture/pipeline-workbench") as refreshed:
                        workbench.get_by_role("button", name="Refresh readiness", exact=True).click()
                    self.assertEqual(refreshed.value.status, 200, refreshed.value.text())
                    payload = refreshed.value.json()
                    self.assertEqual(payload["profile_id"], "fixture")
                    self.assertEqual(payload["items"][0]["job_id"], "a" * 64)
                    self.assertEqual(payload["items"][0]["stage"], "PREPARING")
                    self.assertNotIn("b" * 64, {item["job_id"] for item in payload["items"]})
                    expect(workbench).to_contain_text("Preparation in progress")
                    expect(workbench).not_to_contain_text("Foreign Synthetic")

                    with store.engine.connect() as connection:
                        rows = list(connection.execute(select(m.operations).where(
                            m.operations.c.kind == "PREPARE",
                        )).mappings())
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["entity_id"], "fixture")
                    self.assertEqual(rows[0]["params"]["job_id"], "a" * 64)
                    self.assertEqual(rows[0]["params"]["request_id"], accepted["request_id"])
                    self.assertNotIn("private=synthetic", repr(payload))
                    self.assertNotIn("example.test", repr(payload))
                    self.assertEqual(page_errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated browser API must stop")

            self.assertEqual(external_requests, [])

    @staticmethod
    def _seed_profile_job(store, profile_id, job_id, title, company_name):
        profile_version = store.profile(profile_id)["version"]
        company_id = f"company-{profile_id}"
        route_id = f"route-{profile_id}"
        with store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name=company_name,
                domain=f"{profile_id}.example.test",
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=company_id, requisition=f"REQ-{profile_id}",
                title=title, description="Synthetic role for local browser acceptance.",
                skills=["python"], location="Remote", remote_type="REMOTE",
                attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id=f"match-{profile_id}", job_id=job_id, profile_id=profile_id,
                profile_version=profile_version, score=88, missing=[],
                components={"skills": 88},
            ))
            connection.execute(m.routes.insert().values(
                id=route_id, job_id=job_id, company_id=company_id,
                provider="greenhouse", tenant=f"tenant-{profile_id}",
                requisition=f"REQ-{profile_id}",
                url=f"https://jobs.example.test/{profile_id}?private=synthetic",
            ))


if __name__ == "__main__":
    unittest.main()
