"""Connected acceptance for immutable, user-visible document fact references."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
import json

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class DocumentFactTraceBrowserTests(unittest.TestCase):
    def test_history_shows_only_facts_from_immutable_generation_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'trace.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            unapproved = {
                "id": "unapproved-credential",
                "text": "Unapproved Security Clearance",
                "kind": "certification",
                "source": "unreviewed-import",
                "verified": False,
                "user_approved": False,
                "confidence": 0,
            }
            store.save_profile(dict(PROFILE, facts=[*PROFILE["facts"], unapproved]))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = next(row for row in store.jobs() if row["requisition"] == "B")
            application_id = prepare(store, job["id"], "fixture", root)
            generation_profile = store.profile("fixture")
            original_python = next(f for f in generation_profile["facts"] if f["fact_id"] == "python")
            original_sql = next(f for f in generation_profile["facts"] if f["fact_id"] == "sql")

            current_facts = [
                dict(fact, text="Changed Python fact") if fact["id"] == "python" else fact
                for fact in [*PROFILE["facts"], unapproved]
            ]
            store.save_profile(dict(PROFILE, facts=current_facts))

            app = create_app(
                store,
                "http://127.0.0.1:1",
                root,
                "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Document fact-trace API did not start")
                    time.sleep(0.02)

                with urlopen(
                    f"{origin}/profiles/fixture/applications/{application_id}/documents", timeout=5
                ) as response:
                    self.assertEqual(response.status, 200)
                    history = json.load(response)
                resume = next(row for row in history["documents"] if row["kind"] == "resume")
                self.assertEqual(resume["fact_ids"], ["python", "sql"])
                self.assertEqual(
                    {fact["fact_id"] for fact in resume["facts_used"]},
                    {"python", "sql"},
                )
                self.assertEqual(
                    {fact["fact_id"]: fact["text"] for fact in resume["facts_used"]},
                    {"python": original_python["text"], "sql": original_sql["text"]},
                )
                self.assertEqual(resume["missing_fact_ids"], [])

                external = []
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload()
                    page.reload()
                    navigation = page.get_by_role("button", name="Applications", exact=False)
                    try:
                        expect(navigation.first).to_be_visible(timeout=8000)
                    except AssertionError as error:
                        self.fail(f"Applications navigation missing after startup: {page.locator('body').inner_text()} ({error})")
                    navigation.first.click()
                    page.locator(".job-card").filter(has_text=job["title"]).first.click()
                    document = page.get_by_role("region", name="resume version 1", exact=True)
                    document.get_by_text("Generation details and facts", exact=True).click()
                    expect(document).to_contain_text(original_python["text"])
                    expect(document).to_contain_text(original_sql["text"])
                    expect(document).to_contain_text("Source: fictional-test-profile")
                    expect(document).not_to_contain_text("Changed Python fact")
                    expect(document).not_to_contain_text(unapproved["text"])
                    expect(document).to_contain_text("At generation: verified, approved")
                    self.assertFalse(external)
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
