"""Connected browser acceptance for evidence-based, non-blocking job warnings."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import func, select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
RISK_DESCRIPTION = (
    "Synthetic systems role. Applicants must pay $75 for equipment before an interview. "
    "The recruiter says interviews happen only through Telegram. This exact synthetic "
    "description is reused for a separate unrelated employer."
)


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class JobRiskSignalsBrowserTests(unittest.TestCase):
    def test_risk_evidence_is_limited_unknown_and_non_blocking(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-job-risk-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'risk.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")

            jobs = store.jobs("fixture")
            self.assertGreaterEqual(len(jobs), 2, "Synthetic discovery should return two jobs")
            target = next((row for row in jobs if row["title"] == "Data Engineer"), jobs[0])
            repeated = next(row for row in jobs if row["id"] != target["id"])
            target_company_id = target["company_id"]
            repeated_company_id = "risk-unrelated-synthetic-company"
            risky_url = "http://quick-payroll-alert.xyz/jobs/synthetic-role-242"
            with store.engine.begin() as connection:
                connection.execute(update(m.companies).where(
                    m.companies.c.id == target_company_id,
                ).values(name="Unverified Synthetic Employer", domain=""))
                connection.execute(m.companies.insert().values(
                    id=repeated_company_id,
                    name="Unrelated Synthetic Employer",
                    domain="",
                ))
                connection.execute(update(m.jobs).where(
                    m.jobs.c.id == target["id"],
                ).values(
                    description=RISK_DESCRIPTION,
                    attributes={
                        **target.get("attributes", {}),
                        "salary_min": 10000000,
                        "salary_max": 15000000,
                        "salary_currency": "USD",
                        "salary_period": "YEAR",
                    },
                ))
                connection.execute(update(m.jobs).where(
                    m.jobs.c.id == repeated["id"],
                ).values(
                    company_id=repeated_company_id,
                    description=RISK_DESCRIPTION,
                ))
                connection.execute(update(m.routes).where(
                    m.routes.c.job_id == target["id"],
                ).values(url=risky_url))
                connection.execute(update(m.routes).where(
                    m.routes.c.job_id == repeated["id"],
                ).values(company_id=repeated_company_id))
                connection.execute(update(m.postings).where(
                    m.postings.c.job_id == target["id"],
                ).values(source_url=risky_url, application_url=risky_url))
                connection.execute(update(m.postings).where(
                    m.postings.c.job_id == repeated["id"],
                ).values(application_url=risky_url))

            def table_counts():
                with store.engine.connect() as connection:
                    return {
                        table.name: connection.execute(
                            select(func.count()).select_from(table)
                        ).scalar_one()
                        for table in m.metadata.sorted_tables
                    }

            before = table_counts()
            database = root / "risk.db"
            store.engine.dispose()
            store = Store(f"sqlite:///{database.as_posix()}")
            app = create_app(
                store, "http://127.0.0.1:1", root,
                operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            external_requests = []
            writes = []
            submission_requests = []
            errors = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated job-risk API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script(
                        "localStorage.setItem('mjobs-profile', 'fixture')"
                    )

                    def local_read_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif request.method not in {"GET", "HEAD"}:
                            writes.append((request.method, parsed.path))
                            if any(part in parsed.path.casefold() for part in (
                                "/apply", "/submit", "/mock-run", "/applications",
                            )):
                                submission_requests.append((request.method, parsed.path))
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_read_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    response_jobs = []
                    page.on("response", lambda response: response_jobs.append(response)
                            if response.url.startswith(origin + "/jobs?") else None)
                    page.goto(origin, wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name=re.compile(r"^Jobs\s+\d+$"),
                    ).click()

                    risky_card = page.get_by_role(
                        "button", name=re.compile(re.escape(target["title"])),
                    ).first
                    expect(risky_card).to_be_visible()
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "profile_id=fixture" in response.url) as jobs_response:
                        # The page may have loaded the initial list already; this
                        # fresh title filter guarantees a connected discovery read.
                        page.get_by_label("Job title", exact=True).fill(target["title"])
                    self.assertEqual(jobs_response.value.status, 200)
                    returned = jobs_response.value.json()["jobs"]
                    self.assertIn(target["id"], {row["id"] for row in returned})
                    self.assertEqual(response_jobs[-1].status, 200)
                    page.get_by_role(
                        "button", name=re.compile(re.escape(target["title"])),
                    ).first.click()

                    # The warning panel is an explanatory signal only. Unknown
                    # company identity remains explicitly UNKNOWN; the evidence
                    # quotes local synthetic fields and keeps the limitations visible.
                    potential_risk = page.get_by_role(
                        "region", name=re.compile("Potential Risk", re.I),
                    )
                    expect(potential_risk).to_be_visible()
                    # Wait for the connected API result before snapshotting the evidence.
                    expect(potential_risk).to_contain_text("10,000,000")
                    risk_text = potential_risk.inner_text()
                    body = page.locator("body").inner_text()
                    self.assertIn("10,000,000", risk_text)
                    self.assertIn("$75", risk_text)
                    self.assertIn("Telegram", risk_text)
                    self.assertIn("quick-payroll-alert.xyz", risk_text)
                    self.assertIn("UNKNOWN", risk_text)
                    self.assertIn("exact synthetic description", risk_text)
                    self.assertTrue(any(term in risk_text.casefold() for term in (
                        "not proof", "does not prove", "not a determination",
                        "heuristic", "unverified",
                    )), "Risk display must state limits of its evidence")
                    self.assertNotRegex(risk_text.casefold(), r"\bthis (job|employer) is (a )?scam\b")
                    self.assertNotRegex(risk_text.casefold(), r"\bthis employer is fraudulent\b")
                    expect(page.get_by_text("External submission OFF", exact=True)).to_be_visible()

                    prepare = page.get_by_role("button", name="Prepare documents", exact=True)
                    expect(prepare).to_be_visible()
                    expect(prepare).to_be_enabled()
                    self.assertEqual(writes, [])
                    self.assertEqual(submission_requests, [])

                    # Risk data follows the active profile's discovery scope.
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "profile_id=other" in response.url) as other_jobs:
                        page.get_by_label("Active profile", exact=True).select_option("other")
                    self.assertEqual(other_jobs.value.status, 200)
                    # Discovery rows are shared across selected profiles; the
                    # explanatory endpoint remains bound to a current profile match.
                    cross_profile = page.evaluate("""async ({profileId, jobId}) => {
                      const response = await fetch(`/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/risk-signals`);
                      return response.status;
                    }""", {"profileId": "other", "jobId": target["id"]})
                    self.assertEqual(cross_profile, 404)
                    self.assertEqual(potential_risk.count(), 0)

                    self.assertEqual(external_requests, [])
                    self.assertEqual(writes, [])
                    self.assertEqual(submission_requests, [])
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                final_counts = table_counts()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated job-risk API did not stop")
            self.assertEqual(final_counts, before)


if __name__ == "__main__":
    unittest.main()
