"""T20 connected acceptance for API readiness and safe degraded deployment state."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import os
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from cli.verify_readiness import evaluate_readiness
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.company_discovery.public_client import PublicCompanyClient
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]


class FleetT20ProductJourneyTests(unittest.TestCase):
    def test_api_readiness_and_fixture_data_readback_fail_closed_without_runtime(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-fleet-t20-api-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'journey.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE, id="t20-fixture"))

            with fixture_server() as fixture:
                app = create_app(
                    store,
                    fixture,
                    root,
                    redis_url="redis://127.0.0.1:1/0",
                    operation_mode="FIXTURE",
                )
                with TestClient(app) as client:
                    readiness = client.get("/readiness")
                    self.assertEqual(readiness.status_code, 503)
                    payload = readiness.json()
                    self.assertEqual(payload["status"], "degraded")
                    self.assertEqual(payload["db_backend"], "sqlite")
                    self.assertTrue(payload["checks"]["configuration"])
                    self.assertTrue(payload["checks"]["database"])
                    self.assertFalse(payload["checks"]["queue"])

                    decision = evaluate_readiness(payload, http_status=readiness.status_code)
                    self.assertFalse(decision["ready"])
                    self.assertEqual(decision["checks"]["PostgreSQL reachable"], "NOT_RUN")

                    settings = client.get("/settings")
                    self.assertEqual(settings.status_code, 200)
                    self.assertFalse(settings.json()["external_submission_enabled"])
                    self.assertEqual(
                        client.get("/profiles/t20-fixture").json()["name"],
                        PROFILE["name"],
                    )

            store.engine.dispose()
            reopened = Store(database_url)
            try:
                self.assertEqual(reopened.profile("t20-fixture")["email"], PROFILE["email"])
            finally:
                reopened.engine.dispose()

    @unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                         "Build apps/web and allocate the root browser slot before connected acceptance")
    def test_public_ui_queues_company_discovery_and_reads_worker_result_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-fleet-t20-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            output_dir = root / "outputs"
            env = {
                "MJOBS_DATABASE_URL": store.url,
                "MJOBS_OPERATION_MODE": "PERSONAL",
                "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
                "MJOBS_OUTPUT_DIR": str(output_dir),
            }

            pages = {
                "/robots.txt": "User-agent: *\nAllow: /",
                "/": '<html>Example Systems<a rel="careers" href="/careers">Careers</a></html>',
                "/careers": ('<html>Example Systems<link rel="alternate" '
                             'type="application/feed+json" href="/jobs.json"></html>'),
                "/jobs.json": (
                    '{"version":"https://jsonfeed.org/version/1.1","items":[{'
                    '"id":"role-1","url":"https://employer.example/jobs/1",'
                    '"title":"Python Engineer","content_text":"Python work. '
                    '$90,000 per year.","location":"Remote - United States"}]}'
                ),
                "/jobs/1": (
                    '<script type="application/ld+json">{"@context":"https://schema.org",'
                    '"@type":"JobPosting","identifier":{"@type":"PropertyValue",'
                    '"value":"role-1"},"title":"Python Engineer",'
                    '"url":"https://employer.example/jobs/1"}</script>'
                ),
            }
            fixture_requests = []

            class SyntheticEmployerHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    fixture_requests.append(self.path)
                    body = pages.get(urlsplit(self.path).path)
                    if body is None:
                        self.send_error(404)
                        return
                    content_type = "application/json" if self.path.endswith(".json") else "text/html; charset=utf-8"
                    encoded = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

                def log_message(self, *_):
                    pass

            employer_server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticEmployerHandler)
            employer_thread = threading.Thread(target=employer_server.serve_forever, daemon=True)
            employer_thread.start()
            employer_origin = f"http://127.0.0.1:{employer_server.server_port}"
            employer_fetches = []

            def fetch_synthetic_employer(client, url, deadline):
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "employer.example" or parsed.port not in (None, 443):
                    raise AssertionError(f"Unexpected employer target: {url}")
                employer_fetches.append(url)
                with httpx.Client(timeout=3, trust_env=False) as local_client:
                    local_response = local_client.get(employer_origin + (parsed.path or "/"))
                request = httpx.Request("GET", url)
                return httpx.Response(
                    local_response.status_code,
                    headers=local_response.headers,
                    content=local_response.content,
                    request=request,
                )

            app = create_app(store, "http://127.0.0.1:1", output_dir,
                             redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            api_socket = bound_browser_socket()
            origin = f"http://127.0.0.1:{api_socket.getsockname()[1]}"
            app_server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            app_thread = threading.Thread(target=app_server.run,
                                          kwargs={"sockets": [api_socket]}, daemon=True)
            worker_thread = None
            worker_result = {}
            browser = None
            context = None
            try:
                with patch.dict(os.environ, env), patch.object(
                    PublicCompanyClient, "_request_once", fetch_synthetic_employer
                ):
                    app_thread.start()
                    startup_deadline = time.monotonic() + 10
                    while not app_server.started:
                        if not app_thread.is_alive() or time.monotonic() > startup_deadline:
                            self.fail("T20 isolated API did not start")
                        time.sleep(0.02)

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(viewport={"width": 1365, "height": 1000})
                        external_requests = []
                        app_netloc = urlsplit(origin).netloc

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != app_netloc:
                                external_requests.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.locator("#active-profile").select_option("fixture")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        form = page.get_by_role(
                            "heading", name="Add a company to look for jobs", exact=True
                        ).locator("..")
                        form.get_by_label("Company name").fill("Example Systems")
                        form.get_by_label("Company website (HTTPS)").fill("https://employer.example/")
                        with page.expect_response(
                            lambda response: response.request.method == "POST"
                            and response.url.endswith("/companies/discovery")
                        ) as queued_response:
                            form.get_by_role("button", name="Discover company jobs", exact=True).click()
                        operation_id = queued_response.value.json()["operation_id"]
                        self.assertTrue(operation_id)

                        def run_worker():
                            worker_result["value"] = execute_operation(operation_id)

                        worker_thread = threading.Thread(target=run_worker, daemon=True)
                        worker_thread.start()
                        expect(page.get_by_role("status").filter(has_text="Completed")).to_be_visible(timeout=15000)
                        expect(page.get_by_role("status").filter(
                            has_text="Public company jobs and source information have been updated."
                        )).to_be_visible()

                        page.get_by_role("button", name="Jobs", exact=False).first.click()
                        card = page.locator(".job-card").filter(has_text="Example Systems").first
                        expect(card).to_be_visible()
                        expect(card).to_contain_text("Python Engineer")
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Jobs", exact=False).first.click()
                        reloaded_card = page.locator(".job-card").filter(has_text="Example Systems").first
                        expect(reloaded_card).to_be_visible()
                        expect(reloaded_card).to_contain_text("Python Engineer")
                        self.assertEqual(page_errors, [])
                        self.assertEqual(external_requests, [])
                        settings = page.request.get(origin + "/settings").json()
                        self.assertFalse(settings["external_submission_enabled"])

                    worker_thread.join(timeout=10)
                    self.assertFalse(worker_thread.is_alive(), "T20 company worker did not finish")
                    self.assertEqual(worker_result.get("value", {}).get("status"), "COMPLETED", worker_result)

                    with httpx.Client(base_url=origin, timeout=3, trust_env=False) as api_client:
                        public_jobs = api_client.get("/jobs", params={"profile_id": "fixture"})
                        self.assertEqual(public_jobs.status_code, 200, public_jobs.text)
                        rows = public_jobs.json()["jobs"]
                        self.assertTrue(any(row["title"] == "Python Engineer" for row in rows), rows)
                        activity = api_client.get("/profiles/fixture/audit-history")
                        self.assertEqual(activity.status_code, 200, activity.text)
                        self.assertTrue(any(
                            event.get("summary") == "Company discovered for this profile"
                            for event in activity.json()["events"]
                        ), activity.json())

                self.assertEqual(set(fixture_requests), set(pages))
                self.assertEqual(
                    set(urlsplit(url).path for url in employer_fetches), set(pages)
                )
            finally:
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()
                app_server.should_exit = True
                app_thread.join(timeout=10)
                api_socket.close()
                employer_server.shutdown()
                employer_server.server_close()
                employer_thread.join(timeout=5)
                store.engine.dispose()
                self.assertFalse(app_thread.is_alive(), "T20 isolated API did not stop")
                self.assertFalse(employer_thread.is_alive(), "T20 synthetic employer did not stop")


if __name__ == "__main__":
    unittest.main()
