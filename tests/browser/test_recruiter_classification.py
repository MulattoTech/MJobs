"""Connected local-browser acceptance for advisory recruiter classification."""

import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, now, upsert
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class RecruiterClassificationBrowserTests(unittest.TestCase):
    def test_classification_is_evidence_bound_advisory_and_never_filters_or_writes(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-recruiter-classification-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")

            profile = copy.deepcopy(PROFILE)
            profile["id"] = "recruiter-fixture"
            profile["name"] = "Synthetic Recruiter Classification Profile"
            second_profile = copy.deepcopy(profile)
            second_profile["id"] = "recruiter-fixture-other"
            second_profile["name"] = "Synthetic Other Profile"
            store.save_profile(profile)
            store.save_profile(second_profile)
            scoped_profiles = [
                store.profile(profile["id"]),
                store.profile(second_profile["id"]),
            ]

            jobs = (
                {
                    "id": "recruiter-direct-job",
                    "company_id": "recruiter-direct-company",
                    "company": "Synthetic Direct Employer",
                    "title": "Direct employer fixture",
                    "classification": "DIRECT_EMPLOYER",
                    "subject_type": "COMPANY",
                    "source": "VERIFIED_COMPANY_CONTEXT",
                    "evidence_ref": "synthetic:company-profile",
                },
                {
                    "id": "recruiter-ambiguous-job",
                    "company_id": "recruiter-ambiguous-company",
                    "company": "Synthetic Ambiguous Employer",
                    "title": "Ambiguous employer fixture",
                    "classification": None,
                },
                {
                    "id": "recruiter-staffing-job",
                    "company_id": "recruiter-staffing-company",
                    "company": "Synthetic Staffing Agency Example",
                    "title": "Staffing agency fixture remains visible",
                    "classification": "STAFFING_AGENCY",
                    "source": "SOURCE_PROVIDED_JOB_METADATA",
                    "evidence_ref": "synthetic:posting-employer-label",
                },
            )
            with store.engine.begin() as connection:
                for fixture in jobs:
                    upsert(connection, m.companies, {
                        "id": fixture["company_id"], "name": fixture["company"],
                        "domain": f"{fixture['id']}.example.test",
                    })
                    evidence = []
                    if fixture["classification"]:
                        subject_type = fixture.get("subject_type", "JOB")
                        evidence = [{
                            "classification": fixture["classification"],
                            "subject_type": subject_type,
                            "subject_id": fixture["company_id"] if subject_type == "COMPANY" else fixture["id"],
                            "source": fixture["source"],
                            "evidence_ref": fixture["evidence_ref"],
                            "current": True,
                            "explicit": True,
                            "confidence": 0.91,
                        }]
                    upsert(connection, m.jobs, {
                        "id": fixture["id"], "company_id": fixture["company_id"],
                        "requisition": fixture["id"], "title": fixture["title"],
                        "description": "Synthetic evidence-only browser fixture.",
                        "skills": [], "location": "Remote", "remote_type": "REMOTE",
                        "attributes": {
                            "fictional": True,
                            "recruiter_classification_evidence": evidence,
                        },
                    })
                    for scoped_profile in scoped_profiles:
                        upsert(connection, m.matches, {
                            "id": f"match-{scoped_profile['id']}-{fixture['id']}",
                            "job_id": fixture["id"], "profile_id": scoped_profile["id"],
                            "profile_version": scoped_profile["version"],
                            "score": 72.5, "missing": [], "components": {},
                        })
                    upsert(connection, m.routes, {
                        "id": "route-" + fixture["id"],
                        "job_id": fixture["id"],
                        "company_id": fixture["company_id"],
                        "provider": "fixture",
                        "tenant": "recruiter-classification-browser",
                        "requisition": fixture["id"],
                        "url": f"https://jobs.example.test/apply/{fixture['id']}",
                    })
                    upsert(connection, m.postings, {
                        "id": "posting-" + fixture["id"], "job_id": fixture["id"],
                        "source": "synthetic-source", "source_job_id": fixture["id"],
                        "source_url": f"https://source.example.test/jobs/{fixture['id']}",
                        "application_url": "", "fetched_at": now(),
                        "raw": {"fictional": True},
                    })

            counts_before = {
                "operations": store.count("worker_operations"),
                "applications": store.count("applications"),
            }
            store.engine.dispose()
            store = Store(database_url)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=2))

            def serve_isolated_api():
                with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                    runner.run(server.serve(sockets=[sock]))

            thread = threading.Thread(target=serve_isolated_api, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated recruiter-classification API did not start")
                    time.sleep(0.02)

                external = []
                requests = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 1000})

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            requests.append((route.request.method, parsed.path, parse_qs(parsed.query)))
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'recruiter-fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("recruiter-fixture")

                    visible_jobs = page.locator(".job-card")
                    expect(visible_jobs.filter(has_text="Direct employer fixture")).to_be_visible()
                    expect(visible_jobs.filter(has_text="Ambiguous employer fixture")).to_be_visible()
                    staffing_card = visible_jobs.filter(has_text="Staffing agency fixture remains visible")
                    expect(staffing_card).to_be_visible()

                    def get_classification(job_id, profile_id="recruiter-fixture"):
                        response = page.evaluate("""async ({profileId, jobId}) => {
                          const response = await fetch(`/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/recruiter-classification`);
                          return {status: response.status, body: await response.json()};
                        }""", {"profileId": profile_id, "jobId": job_id})
                        self.assertEqual(response["status"], 200, response)
                        return response["body"]

                    # The endpoint is profile/job-bound and GET-only. Evidence is persisted
                    # in synthetic source postings, not inferred from employer-name text.
                    direct_result = get_classification("recruiter-direct-job")
                    self.assertEqual(direct_result["profile_id"], "recruiter-fixture")
                    self.assertEqual(direct_result["job_id"], "recruiter-direct-job")
                    self.assertEqual(direct_result["classification"], "DIRECT_EMPLOYER")
                    self.assertTrue(direct_result["reasons"])
                    self.assertEqual(direct_result["evidence"][0]["subject_type"], "COMPANY")
                    self.assertEqual(direct_result["evidence"][0]["subject_id"], "recruiter-direct-company")
                    self.assertEqual(direct_result["evidence"][0]["source"], "VERIFIED_COMPANY_CONTEXT")
                    self.assertEqual(direct_result["evidence"][0]["evidence_ref"], "synthetic:company-profile")

                    ambiguous_result = get_classification("recruiter-ambiguous-job")
                    self.assertEqual(ambiguous_result["classification"], "UNKNOWN")
                    self.assertEqual(ambiguous_result["evidence"], [])
                    self.assertTrue(ambiguous_result["unknowns"])

                    # The same identity pair must be echoed under another profile; a result
                    # from the first profile cannot be reused under the second profile path.
                    other = get_classification("recruiter-direct-job", "recruiter-fixture-other")
                    self.assertEqual(other["profile_id"], "recruiter-fixture-other")
                    self.assertEqual(other["job_id"], "recruiter-direct-job")

                    staffing = get_classification("recruiter-staffing-job")
                    self.assertEqual(staffing["classification"], "STAFFING_AGENCY")
                    direct_card = visible_jobs.filter(has_text="Direct employer fixture")
                    direct_card.click()
                    expect(page.locator(".job-detail > h2").first).to_have_text(
                        "Direct employer fixture",
                    )
                    region = page.locator(
                        '.job-detail section[aria-labelledby="recruiter-classification-heading"][data-job-id="recruiter-direct-job"]'
                    )
                    expect(region).to_be_visible()
                    expect(region.get_by_role("button", name="Classify employer relationship", exact=True)).to_be_visible()
                    region.get_by_role("button", name="Classify employer relationship", exact=True).click()
                    expect(region).to_contain_text("DIRECT EMPLOYER")
                    expect(region).to_contain_text("VERIFIED_COMPANY_CONTEXT")
                    expect(region).to_contain_text("synthetic:company-profile")

                    ambiguous_card = visible_jobs.filter(has_text="Ambiguous employer fixture")
                    ambiguous_card.click()
                    expect(page.locator(".job-detail > h2").first).to_have_text(
                        "Ambiguous employer fixture",
                    )
                    region = page.locator(
                        '.job-detail section[aria-labelledby="recruiter-classification-heading"][data-job-id="recruiter-ambiguous-job"]'
                    )
                    expect(region).to_be_visible()
                    region.get_by_role("button", name="Classify employer relationship", exact=True).click()
                    expect(region).to_contain_text("UNKNOWN")
                    expect(region).to_contain_text("No classification evidence is available.")
                    expect(region).not_to_contain_text("DIRECT EMPLOYER")

                    staffing_card.click()
                    expect(page.locator(".job-detail > h2").first).to_have_text(
                        "Staffing agency fixture remains visible",
                    )
                    detail = page.locator(".job-detail")
                    region = page.locator(
                        '.job-detail section[aria-labelledby="recruiter-classification-heading"][data-job-id="recruiter-staffing-job"]'
                    )
                    expect(region).to_be_visible()
                    region.get_by_role("button", name="Classify employer relationship", exact=True).click()
                    expect(region).to_contain_text("STAFFING AGENCY")
                    expect(region).to_contain_text("advisory")
                    expect(region).to_contain_text("does not automatically reject staffing firms")
                    expect(region).to_contain_text("SOURCE_PROVIDED_JOB_METADATA")
                    expect(region).to_contain_text("synthetic:posting-employer-label")
                    expect(detail).to_be_visible()
                    expect(region.get_by_role("button", name="Refresh classification", exact=True)).to_be_visible()
                    self.assertFalse(any(
                        word in (region.inner_text() or "").lower()
                        for word in ("reject this job", "exclude this job", "not eligible")))

                    # An explicit classification request is read-only. No method other
                    # than GET is allowed in this workflow; there are no external origins.
                    expect(page.locator(".job-card").filter(has_text="Staffing agency fixture remains visible")).to_be_visible()
                    self.assertEqual(external, [])
                    self.assertFalse([request for request in requests if request[0] != "GET"], requests)
                    self.assertFalse(any(
                        method != "GET" and path.startswith(("/applications", "/discover", "/sources", "/collect"))
                        for method, path, _ in requests
                    ), requests)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated recruiter-classification API must stop")

            reopened = Store(database_url)
            try:
                self.assertEqual(reopened.count("worker_operations"), counts_before["operations"])
                self.assertEqual(reopened.count("applications"), counts_before["applications"])
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
