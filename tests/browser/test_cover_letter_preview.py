"""Connected, read-only cover-letter preview acceptance on synthetic data."""
from pathlib import Path
import copy
import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
WEB_ENTRY = ROOT / "apps" / "web" / "src" / "main.tsx"
WEB_COMPONENT = ROOT / "apps" / "web" / "src" / "cover-letter-review-preview.tsx"
_WEB_SOURCE = "\n".join(
    path.read_text(encoding="utf-8") for path in (WEB_ENTRY, WEB_COMPONENT) if path.is_file()
)
_API_SOURCE = "\n".join(
    path.read_text(encoding="utf-8") for path in (ROOT / "apps" / "api").rglob("*.py")
)
_PREVIEW_MOUNTED = (
    (ROOT / "apps" / "web" / "dist" / "index.html").is_file()
    and "Cover letter review preview" in _WEB_SOURCE
    and "cover-letter-preview" in _API_SOURCE
)


@unittest.skipUnless(
    _PREVIEW_MOUNTED,
    "NOT_RUN: cover-letter preview UI/API mount is not assembled in this candidate",
)
class CoverLetterPreviewBrowserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-cover-letter-preview-")
        self.root = Path(self.directory.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.database = self.root / "preview.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))
        with fixture_server() as fixture:
            discover(self.store, fixture, "fixture")
        self.job = next(row for row in self.store.jobs("fixture")
                        if row["requisition"] == "B")
        self._add_synthetic_company_context()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _add_synthetic_company_context(self):
        """Attach reviewed synthetic text while keeping its content unverified."""
        content = "Synthetic company description supplied for preview testing."
        context = {
            "id": "synthetic-company-context-v1",
            "company_id": self.job.get("company_id"),
            "company_source_id": "synthetic-company-source",
            "version": 1,
            "content_origin": "USER_PROVIDED",
            "content_status": "UNVERIFIED",
            "content": content,
            "source_url": "https://careers.example.test/about/company",
            "source_host": "careers.example.test",
            "verified_domain": "example.test",
            "source_identity_method": "VERIFIED_EXISTING_DOMAIN",
            "identity_status": "VERIFIED",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "user_reviewed": True,
            "user_approved": True,
            "reviewed_at": "2026-10-09T00:00:00Z",
        }
        attrs = dict(self.job.get("attributes") or {})
        attrs["approved_company_context"] = context
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"])
                               .values(attributes=attrs))
        self.job = self.store.job(self.job["id"], profile_id="fixture")

    def _open(self):
        app = create_app(self.store, "http://127.0.0.1:1", self.output_dir,
                         operation_mode="FIXTURE")
        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                self.fail("Isolated cover-letter preview API did not start")
            time.sleep(0.02)
        return origin, sock, server, thread

    @staticmethod
    def _error_code(response):
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("detail"), dict):
            return body["detail"].get("code")
        return body.get("code") if isinstance(body, dict) else None

    @staticmethod
    def _preview_button(page):
        return page.get_by_role(
            "button", name=re.compile(r"^(Preview cover letter|Refresh preview)$"),
        )

    def test_preview_is_bound_explained_recoverable_and_read_only(self):
        profile_before = copy.deepcopy(self.store.profile("fixture"))
        job_before = self.store.job(self.job["id"], profile_id="fixture")
        documents_before = self.store.count("documents")
        applications_before = self.store.count("applications")
        attempts_before = self.store.count("application_attempts")
        output_before = sorted(path.relative_to(self.output_dir).as_posix()
                               for path in self.output_dir.rglob("*"))

        origin, sock, server, thread = self._open()
        requests, outside, page_errors = [], [], []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                def local_only(route):
                    requests.append((route.request.method, route.request.url))
                    if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                        outside.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", local_only)
                page.set_default_timeout(8000)
                page.goto(origin, wait_until="domcontentloaded")
                page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                page.reload(wait_until="domcontentloaded")
                page.get_by_role("button", name="Jobs", exact=True).click()
                page.locator(".job-card").filter(has_text=self.job["title"]).first.click()

                expect(page.get_by_role(
                    "region", name="Cover letter review preview",
                )).to_be_visible()
                expect(page.get_by_role(
                    "button", name="Preview cover letter", exact=True,
                )).to_be_visible()
                with page.expect_response(
                    lambda response: response.url.endswith("/cover-letter-preview")
                    and response.request.method == "POST"
                ) as preview_response:
                    self._preview_button(page).click()
                result = preview_response.value
                self.assertEqual(result.status, 200, result.text())
                payload = json.loads(result.request.post_data or "{}")
                body = result.json()
                preview = page.get_by_role("region", name="Cover letter preview and evidence review")
                expect(preview).to_be_visible()
                expect(page.get_by_role("button", name="Refresh preview", exact=True)).to_be_visible()

                # The preview identity must be the currently selected synthetic
                # job/profile, and server data must match the request snapshot.
                self.assertEqual(result.url,
                                 f"{origin}/profiles/fixture/jobs/{self.job['id']}/cover-letter-preview")
                self.assertEqual(body["profile_id"], "fixture")
                self.assertEqual(body["job_id"], self.job["id"])
                self.assertEqual(payload["expected_profile_version"], profile_before["version"])
                self.assertEqual(body["profile_version"], profile_before["version"])
                self.assertEqual(payload["expected_job_fingerprint"], body["job_fingerprint"])
                self.assertRegex(body["job_fingerprint"], r"^[0-9a-f]{64}$")
                self.assertEqual(payload["style"], "professional")
                self.assertEqual(payload["length"], "NORMAL")
                self.assertTrue(body["read_only"])
                self.assertFalse(body["document_created"])

                # Fact IDs and provenance must point only to approved, verified
                # synthetic profile facts; the posting explanation stays visible.
                fact_by_id = {fact["id"]: fact for fact in profile_before["facts"]}
                self.assertTrue(body["fact_ids"])
                self.assertTrue(set(body["fact_ids"]).issubset(fact_by_id))
                fact_sources = preview.get_by_role("region", name="Approved fact sources")
                for fact_id in body["fact_ids"]:
                    fact = fact_by_id[fact_id]
                    expect(fact_sources).to_contain_text(fact_id)
                    expect(fact_sources).to_contain_text(f"Source: {fact['source']}")
                    expect(fact_sources).to_contain_text(fact["text"])
                    expect(fact_sources).to_contain_text("Matched job terms:")
                self.assertTrue(body["responsibilities"])
                self.assertIn("Python", body["skills"])
                self.assertIn("SQL", body["skills"])
                self.assertEqual(body["skill_strengths"]["Python"], "REQUIRED")
                self.assertEqual(body["skill_strengths"]["SQL"], "REQUIRED")
                responsibility_evidence = preview.get_by_role(
                    "region", name="Job responsibility provenance",
                )
                expect(responsibility_evidence).to_contain_text(body["responsibilities"][0])
                skill_evidence = preview.get_by_role("region", name="Job skills and strength")
                expect(skill_evidence).to_contain_text("Python — Required")
                expect(skill_evidence).to_contain_text("SQL — Required")
                expect(preview).to_contain_text(self.job["title"])
                expect(preview).to_contain_text(self.job["company"])
                expect(preview).to_contain_text("USER_PROVIDED")
                expect(preview).to_contain_text("CONTENT UNVERIFIED")
                expect(preview).to_contain_text("SOURCE IDENTITY VERIFIED")
                expect(preview).to_contain_text(body["responsibilities"][0])
                expect(preview).to_contain_text("Synthetic company description supplied for preview testing.")
                self.assertEqual(body["company_context"]["identity_status"], "VERIFIED")
                self.assertEqual(body["company_context"]["content_status"], "UNVERIFIED")
                self.assertEqual(body["company_context"]["content_origin"], "USER_PROVIDED")

                # A preview changes no document, application, attempt, profile
                # fact/version, or generated file.
                self.assertEqual(self.store.profile("fixture"), profile_before)
                self.assertEqual(self.store.job(self.job["id"], profile_id="fixture"), job_before)
                self.assertEqual(self.store.count("documents"), documents_before)
                self.assertEqual(self.store.count("applications"), applications_before)
                self.assertEqual(self.store.count("application_attempts"), attempts_before)
                self.assertEqual(sorted(path.relative_to(self.output_dir).as_posix()
                                        for path in self.output_dir.rglob("*")), output_before)

                # Stale profile and job snapshots fail closed. The user can reload
                # current inputs and obtain a new preview without creating a document.
                stale_profile = copy.deepcopy(profile_before)
                stale_profile["name"] = "Revised Synthetic Applicant"
                stale_profile["expected_version"] = profile_before["version"]
                self.store.save_profile(stale_profile)
                with page.expect_response(
                    lambda response: response.url.endswith("/cover-letter-preview")
                    and response.request.method == "POST"
                ) as stale_profile_preview:
                    self._preview_button(page).click()
                stale_profile_result = stale_profile_preview.value
                self.assertEqual(stale_profile_result.status, 409, stale_profile_result.text())
                self.assertEqual(self._error_code(stale_profile_result),
                                 "COVER_LETTER_PREVIEW_PROFILE_STALE")
                self.assertNotIn("preview_text", stale_profile_result.json())
                expect(page.get_by_role("alert").first).to_contain_text(
                    "selected profile or job changed")

                # Refresh the profile in the actual UI; a fresh bound preview
                # should work before the next independent stale-job case.
                page.reload(wait_until="domcontentloaded")
                page.get_by_role("button", name="Jobs", exact=True).click()
                page.locator(".job-card").filter(has_text=self.job["title"]).first.click()
                with page.expect_response(
                    lambda response: response.url.endswith("/cover-letter-preview")
                    and response.request.method == "POST"
                ) as refreshed_profile_preview:
                    self._preview_button(page).click()
                refreshed_profile = refreshed_profile_preview.value
                self.assertEqual(refreshed_profile.status, 200, refreshed_profile.text())
                refreshed_payload = json.loads(refreshed_profile.request.post_data or "{}")
                self.assertEqual(refreshed_payload["expected_profile_version"],
                                 self.store.profile("fixture")["version"])

                changed_job_description = job_before["description"] + "\nSynthetic preview fingerprint revision."
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"])
                                       .values(description=changed_job_description))
                with page.expect_response(
                    lambda response: response.url.endswith("/cover-letter-preview")
                    and response.request.method == "POST"
                ) as stale_job_preview:
                    self._preview_button(page).click()
                stale_job_result = stale_job_preview.value
                self.assertEqual(stale_job_result.status, 409, stale_job_result.text())
                self.assertEqual(self._error_code(stale_job_result),
                                 "COVER_LETTER_PREVIEW_JOB_STALE")
                self.assertNotIn("preview_text", stale_job_result.json())
                expect(page.get_by_role("alert").first).to_contain_text(
                    "selected profile or job changed")

                page.reload(wait_until="domcontentloaded")
                page.get_by_role("button", name="Jobs", exact=True).click()
                page.locator(".job-card").filter(has_text=self.job["title"]).first.click()
                with page.expect_response(
                    lambda response: response.url.endswith("/cover-letter-preview")
                    and response.request.method == "POST"
                ) as recovered_preview:
                    self._preview_button(page).click()
                recovered = recovered_preview.value
                self.assertEqual(recovered.status, 200, recovered.text())
                self.assertEqual(recovered.json()["profile_version"],
                                 self.store.profile("fixture")["version"])
                expect(page.get_by_role("region", name="Cover letter preview and evidence review")).to_be_visible()

                # No generation/preparation/submission endpoint is allowed from
                # the preview interaction, and no browser request left localhost.
                post_paths = [urlsplit(url).path for method, url in requests if method == "POST"]
                self.assertTrue(post_paths)
                self.assertTrue(all(path.endswith("/cover-letter-preview") for path in post_paths),
                                post_paths)
                self.assertFalse(any("submit" in path.casefold() for path in post_paths))
                self.assertEqual(outside, [])
                self.assertEqual(page_errors, [])
                self.assertEqual(self.store.count("documents"), documents_before)
                self.assertEqual(self.store.count("applications"), applications_before)
                self.assertEqual(self.store.count("application_attempts"), attempts_before)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
