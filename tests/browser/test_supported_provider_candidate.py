"""Connected fixture browser acceptance for supported-feed source candidates."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    (ROOT / "apps/web/dist/index.html").is_file(),
    "Build browser assets with `npm run build --prefix apps/web` before browser acceptance",
)
class SupportedProviderCandidateBrowserTests(unittest.TestCase):
    def test_supported_collection_candidate_is_durable_profile_scoped_and_not_recollected_by_ui(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-supported-candidate-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'supported-candidate.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                "email": "other@example.invalid"})

            endpoint = "https://employer.example/jobs.json"
            application_url = "https://jobs.example-ats.invalid/apply/role-1"
            feed_items = [{
                "id": "synthetic-platform-role", "title": "Synthetic Platform Engineer",
                "content_text": "Maintain synthetic application systems.",
                "url": application_url,
            }]
            pages = {
                "https://employer.example/": SimpleNamespace(
                    url="https://employer.example/",
                    text='Synthetic Feed Employer <a href="/careers">Careers</a>',
                    history=(),
                ),
                "https://employer.example/careers": SimpleNamespace(
                    url="https://employer.example/careers",
                    text='<link rel="alternate" type="application/feed+json" href="/jobs.json">',
                    history=(),
                ),
                endpoint: SimpleNamespace(
                    url=endpoint,
                    text=json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": feed_items}),
                    history=(),
                ),
            }
            provider_requests = []

            class SyntheticSupportedTransport:
                fixture_only = True
                bounded_redirects = True

                def __init__(self, origin, check=None):
                    self.origin = origin
                    self.check = check or (lambda: None)

                def allowed(self, url):
                    return url in pages

                def get(self, url):
                    self.check()
                    provider_requests.append(url)
                    if url not in pages:
                        raise AssertionError(f"Unexpected synthetic provider request: {url}")
                    return pages[url]

                def close(self):
                    return None

            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            try:
                with patch.dict(os.environ, MJOBS_DATABASE_URL=store.url,
                                MJOBS_OPERATION_MODE="FIXTURE", MJOBS_MOCK_ORIGIN=origin,
                                MJOBS_REDIS_URL="redis://127.0.0.1:1/0",
                                MJOBS_OUTPUT_DIR=str(root / "outputs")), \
                     patch("workers.tasks.MockClient", SyntheticSupportedTransport):
                    thread.start()
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Isolated supported-candidate API did not start")
                        time.sleep(0.02)

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context()
                        outbound = []
                        worker_results = []

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                outbound.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.set_default_timeout(8000)
                        page.goto(origin, wait_until="domcontentloaded")
                        page.locator("#active-profile").select_option("fixture")
                        queue_response = page.request.post(
                            f"{origin}/companies/discovery",
                            data={"company_name": "Synthetic Feed Employer",
                                  "company_website": "https://employer.example/",
                                  "reason": "USER_ADDED_COMPANY", "profile_id": "fixture",
                                  "request_id": "synthetic-supported-provider-capture"},
                        )
                        self.assertEqual(queue_response.status, 202, queue_response.text())
                        queued = queue_response.json()
                        self.assertEqual(queued["status"], "QUEUED", queued)
                        with patch("workers.tasks.MockClient", SyntheticSupportedTransport):
                            from workers.tasks import execute_operation
                            worker_results.append(execute_operation(queued["operation_id"]))
                        self.assertEqual(len(worker_results), 1)
                        self.assertEqual(worker_results[0]["status"], "COMPLETED", worker_results[0])

                        # The same collection event can be replayed at the import boundary;
                        # its profile-scoped capture key must leave one durable observation.
                        with store.engine.connect() as connection:
                            source = connection.execute(select(m.company_sources).where(
                                m.company_sources.c.endpoint == endpoint
                            )).mappings().one()
                        self.assertEqual(import_company_feed_jobs(
                            store, company_id=source["company_id"], source_id=source["id"],
                            provider=source["provider"], tenant=source["tenant"], endpoint=endpoint,
                            strategy="PUBLIC_JSON", profile_id="fixture", items=feed_items,
                        ), 1)

                        page.reload(wait_until="domcontentloaded")
                        page.locator("#active-profile").select_option("fixture")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        candidate = page.get_by_role(
                            "article", name="Source candidate jobs.example-ats.invalid")
                        expect(candidate).to_be_visible()
                        expect(candidate).to_contain_text("SUPPORTED COLLECTION")
                        expect(candidate).to_contain_text(endpoint)
                        expect(candidate).to_contain_text(application_url)
                        expect(candidate).to_contain_text("Collection is disabled")
                        expect(candidate.get_by_role(
                            "button", name="Enable collection", exact=True
                        )).to_have_count(0)
                        expect(page.get_by_role(
                            "article", name="Source candidate jobs.example-ats.invalid")).to_have_count(1)
                        page.locator("#active-profile").select_option("other")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        expect(page.get_by_text(
                            "No source candidates need review for this profile.", exact=True
                        )).to_be_visible()
                        self.assertEqual(outbound, [])
                        browser.close()

                with store.engine.connect() as connection:
                    candidates = connection.execute(select(m.source_candidates)).mappings().all()
                    observations = connection.execute(
                        select(m.source_candidate_observations)
                    ).mappings().all()
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["profile_id"], "fixture")
                self.assertEqual(candidates[0]["domain"], "jobs.example-ats.invalid")
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["profile_id"], "fixture")
                self.assertEqual(observations[0]["capture_method"], "SUPPORTED_COLLECTION")
                self.assertEqual(provider_requests, [
                    "https://employer.example/",
                    "https://employer.example/careers",
                    endpoint,
                ])
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
