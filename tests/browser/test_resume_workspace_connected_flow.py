"""Synthetic Chromium acceptance for reviewed resume workspace persistence/export."""
from importlib.util import find_spec
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


class ResumeWorkspaceConnectedFlowTests(unittest.TestCase):
    def test_preview_review_save_history_and_exact_txt_download(self):
        if not (ROOT / "apps" / "web" / "dist" / "index.html").is_file():
            self.skipTest("NOT_RUN: build the web app before browser acceptance")
        if find_spec("playwright") is None or find_spec("sqlalchemy") is None:
            self.skipTest("NOT_RUN: local browser dependencies unavailable")
        from playwright.sync_api import expect, sync_playwright
        import uvicorn
        from apps.api.main import create_app
        from fixtures.browser_ports import bound_browser_socket
        from fixtures.mock_server import fixture_server
        from packages.persistence.store import Store
        from services.discovery.pipeline import discover
        from tests.integration.test_spine import PROFILE

        with tempfile.TemporaryDirectory(prefix="mjobs-resume-workspace-browser-") as directory:
            output = Path(directory)
            store = Store(f"sqlite:///{(output / 'workspace.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = dict(PROFILE, id="resume-workspace-browser-profile", name="Synthetic Resume User", facts=[
                {"id":"approved-experience","kind":"experience","text":"Maintained approved production systems.","source":"synthetic browser fixture","verified":True,"user_approved":True,"confidence":100},
                {"id":"approved-linux","kind":"skill","text":"Linux administration.","source":"synthetic browser fixture","verified":True,"user_approved":True,"confidence":100},
            ])
            store.save_profile(profile)
            original_facts = copy.deepcopy(store.profile(profile["id"])["facts"])
            with fixture_server() as origin:
                discover(store, origin, profile["id"])
            job = store.jobs(profile["id"])[0]
            sock = bound_browser_socket()
            server_origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", output, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("local API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    external = []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(server_origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(server_origin)
                    page.get_by_label("Active profile", exact=True).select_option(profile["id"])
                    page.get_by_role("navigation").get_by_role("button", name="Jobs", exact=False).click()
                    page.locator(".job-card").filter(has_text=job["title"]).first.click()
                    workspace = page.get_by_role("region", name="Resume workspace", exact=True)
                    expect(workspace).to_be_visible()
                    expect(workspace.get_by_label("Resume template", exact=True).locator("option")).to_have_count(3)
                    workspace.get_by_role("button", name="Preview this resume", exact=True).click()
                    preview_text = workspace.locator("pre").first
                    expect(preview_text).to_contain_text("Maintained approved production systems.")
                    expect(workspace.get_by_role("region", name="Keyword coverage analysis", exact=True)).to_be_visible()
                    self.assertEqual(store.count("resume_workspace_revisions"), 0)
                    self.assertEqual(store.count("documents"), 0)
                    self.assertEqual(store.count("applications"), 0)
                    reviewed_text = preview_text.inner_text()
                    workspace.get_by_label("I reviewed this resume workspace preview", exact=True).check()
                    workspace.get_by_role("button", name="Save reviewed version", exact=True).click()
                    expect(workspace.get_by_text("Version 1", exact=False)).to_be_visible()
                    with page.expect_download() as downloaded:
                        workspace.get_by_role("link", name="Download exact saved TXT", exact=True).click()
                    file = downloaded.value
                    self.assertTrue(file.suggested_filename.endswith(".txt"))
                    self.assertEqual(Path(file.path()).read_text(encoding="utf-8"), reviewed_text)
                    self.assertEqual(store.count("resume_workspace_revisions"), 1)
                    page.reload()
                    page.get_by_label("Active profile", exact=True).select_option(profile["id"])
                    page.get_by_role("navigation").get_by_role("button", name="Jobs", exact=False).click()
                    page.locator(".job-card").filter(has_text=job["title"]).first.click()
                    workspace = page.get_by_role("region", name="Resume workspace", exact=True)
                    expect(workspace.get_by_text("Version 1", exact=False)).to_be_visible()
                    self.assertEqual(store.profile(profile["id"])["facts"], original_facts)
                    self.assertEqual(store.count("documents"), 0)
                    self.assertEqual(store.count("applications"), 0)
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
