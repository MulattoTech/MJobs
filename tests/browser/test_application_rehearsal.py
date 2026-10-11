"""Connected browser acceptance for the local-only application rehearsal."""
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
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
GREENHOUSE_URL = "https://boards.greenhouse.io/example/jobs/1234567"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationRehearsalBrowserTests(unittest.TestCase):
    def test_review_receipt_is_connected_local_and_readable_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-rehearsal-browser-") as directory:
            root = Path(directory)
            database = root / "rehearsal.db"
            database_url = f"sqlite:///{database.as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            application = store.application(application_id)
            with store.engine.begin() as connection:
                connection.execute(m.routes.update().where(
                    m.routes.c.id == application["route_id"],
                ).values(provider="greenhouse", tenant="example", url=GREENHOUSE_URL))
                connection.execute(m.applications.update().where(
                    m.applications.c.id == application_id,
                ).values(target="REAL"))
                connection.execute(m.audit.insert().values(
                    id=identity("synthetic-form-review", application_id),
                    entity_id=application_id,
                    action="APPLICATION_FORM_REVIEW",
                    payload={
                        "operation_id": "synthetic-form-review-operation",
                        "request_id": "synthetic-form-review-request",
                        "input_fingerprint": "a" * 64,
                        "profile_id": "fixture",
                        "route_id": application["route_id"],
                        "provider": "greenhouse",
                        "analysis": {
                            "status": "ANALYZED",
                            "reason": None,
                            "fields": [
                                {"id": "email", "name": "email", "type": "email",
                                 "required": True, "review_reason": None},
                                {"id": "custom_question", "name": "custom_question",
                                 "type": "text", "required": True,
                                 "review_reason": "AMBIGUOUS_FIELD"},
                            ],
                        },
                    },
                    timestamp=now(),
                ))

            app = create_app(store, "http://127.0.0.1:1", root,
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
                        self.fail("Isolated rehearsal API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
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
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    card = board.get_by_role("region", name="Not tracked", exact=True).get_by_role(
                        "button", name=job["title"], exact=False,
                    )
                    expect(card).to_be_visible()
                    card.click()

                    panel = page.get_by_role("region", name="Local application rehearsal")
                    expect(panel).to_contain_text("External submission is disabled")
                    expect(panel.get_by_label("email rehearsal value")).to_have_text(PROFILE["email"])
                    expect(panel).to_contain_text("custom_question")
                    expect(panel).to_contain_text("Why this is unresolved")
                    checkbox = panel.get_by_label("I have reviewed these rehearsal values locally")
                    checkbox.check()
                    panel.get_by_role("button", name="Record local review receipt").click()
                    expect(panel.get_by_role("status").filter(
                        has_text="Review receipt recorded locally",
                    )).to_be_visible()
                    expect(panel).not_to_contain_text("Submitted")

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    board.get_by_role("region", name="Not tracked", exact=True).get_by_role(
                        "button", name=job["title"], exact=False,
                    ).click()
                    panel = page.get_by_role("region", name="Local application rehearsal")
                    expect(panel.get_by_role("status").filter(
                        has_text="Review receipt recorded locally",
                    )).to_be_visible()
                    self.assertFalse(external, external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
