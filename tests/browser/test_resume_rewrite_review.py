"""Browser acceptance for read-only, user-reviewed resume rewrite previews."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import func, select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.tailor import target_fingerprint


ROOT = Path(__file__).resolve().parents[2]
@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class ResumeRewriteReviewBrowserTests(unittest.TestCase):
    def test_preview_review_tokens_staleness_and_reload_are_read_only(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-resume-rewrite-review-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'rewrite-review.sqlite').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            original_fact = {
                "id": "experience",
                "kind": "experience",
                "text": "Supported Cisco CCNA routing for 10 sites since 2020.",
                "source": "Synthetic reviewed employment record",
                "verified": True,
                "user_approved": True,
                "confidence": 95,
            }
            skill_fact = {
                "id": "linux",
                "kind": "skill",
                "text": "Linux",
                "source": "Synthetic reviewed skills inventory",
                "verified": True,
                "user_approved": True,
                "confidence": 95,
            }
            store.save_profile({
                "id": "fixture",
                "name": "Synthetic Candidate",
                "email": "synthetic@example.test",
                "facts": [original_fact, skill_fact],
                "answers": [],
                "preferences": {},
            })
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs(profile_id="fixture")[0]
            job_fingerprint = target_fingerprint(store.job(job["id"], "fixture"))
            documents_before = self.counts(store)
            facts_before = deepcopy(store.profile("fixture")["facts"])

            output_dir = root / "documents"
            output_dir.mkdir()
            app = create_app(
                store,
                "http://127.0.0.1:1",
                output_dir,
                "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Synthetic resume rewrite review API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external_requests = []
                    submission_requests = []
                    prepare_requests = []

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                            return
                        if "/submit" in parsed.path:
                            submission_requests.append(route.request.url)
                        if parsed.path == "/applications" and route.request.method == "POST":
                            prepare_requests.append(route.request.post_data_json)
                            route.abort()
                            return
                        route.continue_()

                    context.route("**/*", local_only)
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")
                    page = context.new_page()
                    page.on("response", lambda response: print("ALIGNMENT_RESPONSE", response.status, response.url) if "resume-alignment-preview" in response.url else None)
                    page.on("requestfailed", lambda request: print("ALIGNMENT_FAILED", request.url, request.failure) if "resume-alignment-preview" in request.url else None)
                    page.set_default_timeout(10000)
                    page.goto(origin, wait_until="domcontentloaded")

                    # Load one exact approved source fact into the existing editor.
                    self.open_resume_rewrite_editor(page, job["title"], original_fact["text"])
                    proposed = "Maintained Cisco CCNA network routing across 10 locations since 2020."
                    page.get_by_label(
                        "Rewrite this experience: " + original_fact["text"], exact=True,
                    ).fill(proposed)

                    # A source-fact change after loading the source snapshot
                    # must fail closed. Reloading binds the preview to the updated
                    # approved fact and its fresh profile version.
                    current = store.profile("fixture")
                    changed = deepcopy(current)
                    changed["facts"] = deepcopy(current["facts"])
                    changed["facts"][0]["text"] = "Supported Cisco CCNA network routing for 12 sites since 2020."
                    changed["expected_version"] = current["version"]
                    store.save_profile(changed, expected_version=current["version"])
                    facts_before = deepcopy(store.profile("fixture")["facts"])
                    updated_fact = next(fact for fact in facts_before if fact["id"] == "experience")
                    proposed = "Maintained Cisco CCNA network routing across 12 locations since 2020."
                    stale_endpoint = (
                        f"{origin}/profiles/fixture/jobs/{job['id']}/resume-rewrite-review-preview"
                    )
                    with page.expect_response(
                        lambda response: response.url == stale_endpoint
                        and response.request.method == "POST"
                    ) as stale_preview:
                        page.get_by_role(
                            "button", name="Preview exact resume rewrites", exact=True,
                        ).click()
                    self.assertEqual(stale_preview.value.status, 409, stale_preview.value.text())
                    expect(page.get_by_role("alert")).to_be_visible()
                    self.assertEqual(self.counts(store), documents_before)
                    self.assertEqual(store.profile("fixture")["facts"], facts_before)
                    self.assertEqual(prepare_requests, [])

                    page.reload(wait_until="domcontentloaded")
                    self.open_resume_rewrite_editor(page, job["title"], updated_fact["text"])
                    preview = page.get_by_role(
                        "region", name="Resume rewrite review preview", exact=True,
                    )
                    acknowledgement = page.get_by_label(
                        "I reviewed the proposed wording for fact experience", exact=True,
                    )
                    proposed_box = page.get_by_label(
                        "Rewrite this experience: " + updated_fact["text"], exact=True,
                    )
                    proposed_box.fill(proposed)
                    expect(acknowledgement).not_to_be_checked()

                    with page.expect_response(
                        lambda response: response.url == stale_endpoint
                        and response.request.method == "POST"
                    ) as successful_preview:
                        page.get_by_role(
                            "button", name="Preview exact resume rewrites", exact=True,
                        ).click()
                    self.assertEqual(successful_preview.value.status, 200, successful_preview.value.text())
                    payload = successful_preview.value.request.post_data_json
                    result = successful_preview.value.json()
                    self.assertEqual(payload["selected_fact_ids"], ["experience"])
                    self.assertEqual(payload["rewrites"], [{"fact_id": "experience", "text": proposed}])
                    self.assertEqual(payload["expected_profile_version"], store.profile("fixture")["version"])
                    self.assertEqual(payload["expected_job_fingerprint"], job_fingerprint)
                    self.assertEqual(result["profile_id"], "fixture")
                    self.assertEqual(result["profile_version"], store.profile("fixture")["version"])
                    self.assertEqual(result["job_id"], job["id"])
                    self.assertEqual(result["job_fingerprint"], job_fingerprint)
                    self.assertTrue(result["read_only"])
                    self.assertTrue(result["review_required"])
                    self.assertEqual(result["semantic_equivalence"], "USER_REVIEW_REQUIRED")
                    [rewrite] = result["rewrites"]
                    self.assertEqual(rewrite["fact_id"], "experience")
                    self.assertEqual(rewrite["fact_kind"], "experience")
                    self.assertEqual(rewrite["source_text"], updated_fact["text"])
                    self.assertEqual(rewrite["source_sha256"], sha256(updated_fact["text"].encode()).hexdigest())
                    self.assertEqual(rewrite["proposed_text"], proposed)
                    self.assertEqual(rewrite["protected_token_audit"]["missing"], [])

                    expect(preview).to_contain_text("Approved source text")
                    expect(preview).to_contain_text("User-authored proposed wording")
                    expect(preview).to_contain_text(updated_fact["text"])
                    expect(preview).to_contain_text(proposed)
                    expect(preview).to_contain_text("USER_REVIEW_REQUIRED")
                    expect(acknowledgement).not_to_be_checked()
                    expect(acknowledgement).to_be_enabled()

                    # Acknowledgement is local review state. It does not save a
                    # rewrite or create an application/document; this test never
                    # invokes the distinct PREPARE action.
                    acknowledgement.check()
                    expect(acknowledgement).to_be_checked()
                    self.assertEqual(store.profile("fixture")["facts"], facts_before)
                    self.assertEqual(self.counts(store), documents_before)
                    self.assertEqual(prepare_requests, [])

                    # Reload restores the persisted source fact, resets temporary
                    # acknowledgement, and permits a fresh side-by-side preview.
                    page.reload(wait_until="domcontentloaded")
                    self.open_resume_rewrite_editor(page, job["title"], updated_fact["text"])
                    page.get_by_label(
                        "Rewrite this experience: " + updated_fact["text"], exact=True,
                    ).fill(proposed)
                    with page.expect_response(
                        lambda response: response.url == stale_endpoint
                        and response.request.method == "POST"
                    ) as reload_preview:
                        page.get_by_role(
                            "button", name="Preview exact resume rewrites", exact=True,
                        ).click()
                    self.assertEqual(reload_preview.value.status, 200, reload_preview.value.text())
                    expect(page.get_by_role(
                        "region", name="Resume rewrite review preview", exact=True,
                    )).to_contain_text(proposed)
                    acknowledgement = page.get_by_label(
                        "I reviewed the proposed wording for fact experience", exact=True,
                    )
                    expect(acknowledgement).not_to_be_checked()

                    # Protected credential, date, and numeric tokens cannot be
                    # acknowledged when the user-authored wording omits them.
                    missing_cases = (
                        ("credential", "Supported network routing for 12 sites since 2020.", "NAMED_CREDENTIAL"),
                        ("date", "Supported Cisco CCNA routing for 12 sites.", "DATE"),
                        ("number", "Supported Cisco CCNA routing since 2020.", "NUMBER"),
                    )
                    for case, invalid_wording, missing_type in missing_cases:
                        with self.subTest(missing_token=case):
                            page.get_by_label(
                                "Rewrite this experience: " + updated_fact["text"], exact=True,
                            ).fill(invalid_wording)
                            with page.expect_response(
                                lambda response: response.url == stale_endpoint
                                and response.request.method == "POST"
                            ) as token_preview:
                                page.get_by_role(
                                    "button", name="Preview exact resume rewrites", exact=True,
                                ).click()
                            self.assertEqual(token_preview.value.status, 200, token_preview.value.text())
                            audit = token_preview.value.json()["rewrites"][0]["protected_token_audit"]
                            self.assertTrue(any(item["type"] == missing_type for item in audit["missing"]), audit)
                            expect(page.get_by_role("region", name="Resume rewrite review preview", exact=True))\
                                .to_contain_text("Missing " + missing_type)
                            expect(page.get_by_label(
                                "I reviewed the proposed wording for fact experience", exact=True,
                            )).to_be_disabled()
                            self.assertEqual(store.profile("fixture")["facts"], facts_before)
                            self.assertEqual(self.counts(store), documents_before)
                            self.assertEqual(prepare_requests, [])

                    self.assertEqual(submission_requests, [])
                    self.assertEqual(external_requests, [])
                    self.assertEqual(store.profile("fixture")["facts"], facts_before)
                    self.assertEqual(self.counts(store), documents_before)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    @staticmethod
    def open_resume_rewrite_editor(page, job_title, source_text):
        page.locator(".job-grid .job-list .job-card").filter(has_text=job_title).first.click()
        detail = page.locator(".job-grid .job-detail")
        # The job and profile load independently. Wait for the profile-bound
        # workspace so a late profile read cannot reset the resume controls.
        expect(detail.get_by_role("heading", name="Resume workspace", exact=True)).to_be_visible()
        page.get_by_label("Order approved skills for this job", exact=True).check()
        choose = detail.get_by_label("Choose resume facts", exact=True).last
        choose.check()
        fact = detail.get_by_label("Include " + source_text, exact=True)
        expect(fact).to_be_visible()
        fact.check()
        expect(detail.get_by_label("Rewrite this experience: " + source_text, exact=True)).to_be_visible()

    @staticmethod
    def counts(store):
        with store.engine.connect() as connection:
            return {
                table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (m.documents, m.applications, m.operations)
            }


if __name__ == "__main__":
    unittest.main()
