"""Connected UI reuses company ATS discovery metadata without rediscovery."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import register_ats_tenant
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationKnowledgeDisplayBrowserTests(unittest.TestCase):
    def test_prepared_application_explains_persisted_ats_knowledge_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-ats-knowledge-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'application-knowledge.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(dict(PROFILE))
            with store.engine.begin() as connection:
                upsert(connection, m.companies,
                       {"id": "company", "name": "Acme Systems", "domain": "acme.example"})
                upsert(connection, m.jobs, {
                    "id": "job", "company_id": "company", "requisition": "role-1",
                    "title": "Systems Administrator",
                    "description": "Responsibilities: Manage Python systems.",
                    "skills": ["Windows", "Linux"], "location": "Remote",
                    "remote_type": "REMOTE",
                })
                upsert(connection, m.routes, {
                    "id": "route", "job_id": "job", "company_id": "company",
                    "provider": "rss", "tenant": "acme", "requisition": "role-1",
                    "url": "https://jobs.example.test/role-1",
                })
            tenant = register_ats_tenant(
                store, "company", provider="greenhouse", tenant_identifier="acme",
                confidence=0.97, evidence=["STORED_DISCOVERY_EVIDENCE"],
            )
            try:
                application_id = prepare(store, "job", "fixture", root)
            except BaseException:
                store.engine.dispose()
                raise
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Application knowledge API did not start")
                    time.sleep(0.02)

                errors, external = [], []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    nav = page.get_by_role("navigation", name="Main navigation")
                    nav.get_by_role("button").filter(has_text="Applications").click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    card = pipeline.get_by_role(
                        "button", name="Systems Administrator", exact=False,
                    )
                    expect(card).to_be_visible()
                    card.click()

                    knowledge = page.get_by_role(
                        "region", name="ATS application adapter knowledge",
                    )
                    expect(knowledge).to_contain_text("Greenhouse")
                    expect(knowledge).to_contain_text("acme")
                    expect(knowledge).to_contain_text("Company discovery")
                    expect(knowledge).to_contain_text(
                        "current application URL has not been verified",
                    )
                    expect(knowledge).to_contain_text(
                        "No supported MJobs application adapter is available",
                    )
                    self.assertEqual(context.request.get(
                        origin + f"/profiles/fixture/applications/{application_id}",
                    ).json()["adapter_knowledge"]["ats_tenant_id"], tenant["id"])

                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    nav = page.get_by_role("navigation", name="Main navigation")
                    nav.get_by_role("button").filter(has_text="Applications").click()
                    page.get_by_role("region", name="Application pipeline").get_by_role(
                        "button", name="Systems Administrator", exact=False,
                    ).click()
                    knowledge = page.get_by_role(
                        "region", name="ATS application adapter knowledge",
                    )
                    expect(knowledge).to_contain_text("Greenhouse")
                    expect(knowledge).to_contain_text("External application submission remains OFF")
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    self.assertFalse(context.request.get(origin + "/health").json()[
                        "external_submission_enabled"])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Application knowledge server must stop")


if __name__ == "__main__":
    unittest.main()
