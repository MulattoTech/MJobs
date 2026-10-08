"""Connected browser acceptance for explicit role-lens document generation."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.career_profile.story_bank import create_story, review_story
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE as BASE_PROFILE


ROOT = Path(__file__).resolve().parents[2]
PROFILE = {
    **deepcopy(BASE_PROFILE),
    "facts": [
        {"id": "linux", "kind": "experience",
         "text": "Maintained Linux servers and resolved production incidents.",
         "source": "Synthetic reviewed work history", "verified": True,
         "user_approved": True, "confidence": 100},
        {"id": "cloud", "kind": "project",
         "text": "Built AWS infrastructure with Terraform and configured IAM.",
         "source": "Synthetic reviewed project record", "verified": True,
         "user_approved": True, "confidence": 100},
        {"id": "support", "kind": "accomplishment",
         "text": "Resolved customer support tickets and documented repeat solutions.",
         "source": "Synthetic reviewed support record", "verified": True,
         "user_approved": True, "confidence": 100},
        {"id": "security", "kind": "accomplishment",
         "text": "Audited access controls and patched reported vulnerabilities.",
         "source": "Synthetic reviewed security record", "verified": True,
         "user_approved": True, "confidence": 100},
    ],
}


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class RoleLensGenerationBrowserTests(unittest.TestCase):
    def test_selected_lens_is_named_in_saved_documents_and_remains_local(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-role-lens-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'role-lens-browser.sqlite').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")

            stale = create_story(store, "fixture", {
                "kind": "STAR_STORY", "title": "Stale Linux story",
                "text": "I maintained Linux hosts under older evidence.",
                "fact_ids": ["linux"], "tags": ["systems"],
                "allowed_uses": ["RESUME", "COVER_LETTER"],
            })
            stale = review_story(
                store, "fixture", stale["story_id"], True, None,
                expected_revision=stale["revision"],
            )
            current = store.profile("fixture")
            changed = {
                **deepcopy(PROFILE),
                "id": "fixture",
                "name": current["name"],
                "email": current["email"],
                "facts": [
                    {**fact, "text": "Maintained Linux hosts and resolved production incidents."}
                    if fact["id"] == "linux" else fact
                    for fact in PROFILE["facts"]
                ],
                "expected_version": current["version"],
            }
            store.save_profile(changed, expected_version=current["version"])
            approved = create_story(store, "fixture", {
                "kind": "STAR_STORY", "title": "Current Linux story",
                "text": "I maintained current Linux hosts and resolved production incidents.",
                "fact_ids": ["linux"], "tags": ["systems"],
                "allowed_uses": ["RESUME", "COVER_LETTER"],
            })
            approved = review_story(
                store, "fixture", approved["story_id"], True, None,
                expected_revision=approved["revision"],
            )
            pending = create_story(store, "fixture", {
                "kind": "STAR_STORY", "title": "Pending cloud story",
                "text": "I built a synthetic cloud platform.",
                "fact_ids": ["cloud"], "tags": ["cloud"],
                "allowed_uses": ["RESUME", "COVER_LETTER"],
            })

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Role-lens isolated API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external_requests = []
                    submission_requests = []

                    def local_only(route):
                        request_url = route.request.url
                        if urlsplit(request_url).netloc != urlsplit(origin).netloc:
                            external_requests.append(request_url)
                            route.abort()
                        else:
                            if "/submit" in urlsplit(request_url).path:
                                submission_requests.append(request_url)
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")

                    navigation = page.get_by_role("navigation", name="Main navigation")
                    navigation.get_by_role("button", name="Jobs", exact=False).click()
                    page.locator(".job-card").filter(has_text="Data Engineer").click()
                    lens_picker = page.get_by_label("Role lens for this job", exact=True)
                    lens_picker.select_option(label="Systems · version 1")
                    expect(page.get_by_text(
                        "Selected lens: Systems (version 1) is confirmed for this job.",
                        exact=True,
                    )).to_be_visible()

                    with page.expect_response(
                        lambda response: response.url == origin + "/applications"
                        and response.request.method == "POST"
                    ) as queued:
                        page.get_by_role("button", name="Prepare documents", exact=True).click()
                    operation = queued.value.json()
                    request_body = queued.value.request.post_data_json
                    self.assertEqual(request_body.get("role_lens_id"), "systems")
                    self.assertEqual(request_body.get("expected_role_lens_version"), 1)

                    with patch.dict(
                        "os.environ", MJOBS_DATABASE_URL=database_url,
                        MJOBS_MOCK_ORIGIN="http://127.0.0.1:1",
                        MJOBS_OUTPUT_DIR=str(root), MJOBS_OPERATION_MODE="FIXTURE",
                    ):
                        from workers.tasks import execute_operation
                        outcome = execute_operation(operation["operation_id"])
                    self.assertEqual(outcome["status"], "COMPLETED", outcome)

                    application_id = operation["application_id"]
                    response = context.request.get(
                        origin + f"/profiles/fixture/applications/{application_id}/documents")
                    self.assertEqual(response.status, 200, response.text())
                    documents = response.json()["documents"]
                    resume = next(row for row in documents if row["kind"] == "resume")
                    saved_lens = resume["generation_context"]["role_lens"]
                    self.assertEqual(saved_lens["id"], "systems")
                    self.assertEqual(saved_lens["version"], 1)
                    self.assertEqual(saved_lens["display_name"], "Systems")
                    story_ids = {item["story_id"] for item in saved_lens["source_refs"]["stories"]}
                    self.assertIn(approved["story_id"], story_ids)
                    self.assertNotIn(stale["story_id"], story_ids)
                    self.assertNotIn(pending["story_id"], story_ids)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    navigation.get_by_role("button", name="Applications", exact=False).click()
                    page.locator(".job-card").filter(has_text="Data Engineer").click()
                    saved_document = page.get_by_role("region", name="resume version 1", exact=True)
                    expect(saved_document).to_contain_text("Systems")
                    expect(saved_document).to_contain_text(re.compile(r"v1", re.IGNORECASE))
                    self.assertEqual(external_requests, [])
                    self.assertEqual(submission_requests, [])
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
