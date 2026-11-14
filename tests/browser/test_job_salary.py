"""Connected browser proof that canonical API salary is shown in job detail."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class JobSalaryBrowserTests(unittest.TestCase):
    def test_real_api_canonical_salary_is_displayed_in_job_detail(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-job-salary-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'salary.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(
                    attributes={**job["attributes"], "salary_min": 80000,
                                "salary_max": 110000, "salary_currency": "USD",
                                "salary_period": "YEAR", "salary_source": "SOURCE_PROVIDED"},
                ))

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error",
            ))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external_requests = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated salary API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    response = page.goto(origin, wait_until="domcontentloaded")
                    self.assertIsNotNone(response)
                    self.assertEqual(response.status, 200)
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    with page.expect_response(lambda item:
                            item.url == origin + f"/jobs/{job['id']}?profile_id=fixture") as detail_response:
                        page.locator(".job-card").filter(has_text=job["title"]).click()
                    api_job = detail_response.value.json()
                    self.assertEqual(api_job["salary_min"], 80000)
                    self.assertEqual(api_job["salary_max"], 110000)
                    self.assertEqual(api_job["salary_currency"], "USD")
                    self.assertEqual(api_job["salary_period"], "YEAR")
                    self.assertEqual(api_job["salary_source"], "SOURCE_PROVIDED")
                    expect(page.get_by_label("Salary range", exact=True)).to_contain_text(
                        "USD 80,000–110,000 per year",
                    )
                    expect(page.get_by_label("Salary range", exact=True)).to_contain_text(
                        "Source-reported salary range (not independently verified)",
                    )
                    # A later canonical clear must suppress stale legacy salary,
                    # not let a parallel renderer bring that value back.
                    with store.engine.begin() as connection:
                        connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(
                            attributes={**job["attributes"], "salary_min": None,
                                        "salary_max": None, "salary_currency": None,
                                        "salary_period": None, "salary_source": None,
                                        "salary": [{"min": 70000, "max": 95000,
                                                    "currency": "EUR", "period": "YEAR"}]},
                        ))
                    with page.expect_response(lambda item:
                            item.url == origin + f"/jobs/{job['id']}?profile_id=fixture") as cleared_response:
                        page.locator(".job-card").filter(has_text=job["title"]).click()
                    cleared_job = cleared_response.value.json()
                    self.assertIsNone(cleared_job["salary_min"])
                    self.assertIsNone(cleared_job["salary_max"])
                    self.assertEqual(cleared_job["attributes"]["salary"][0]["currency"], "EUR")
                    expect(page.get_by_label("Salary range", exact=True)).to_have_count(0)
                    expect(page.locator(".job-detail")).not_to_contain_text("EUR 70000–95000")
                    self.assertEqual(external_requests, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()
