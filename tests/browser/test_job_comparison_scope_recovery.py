"""Browser recovery acceptance for scoped job comparisons.

This requires the root lane to mount JobComparisonWorkspace and rebuild the
web bundle. The test stays skipped until both the connected API and UI exist.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket


ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    "candidate": {
        "id": "candidate", "name": "Synthetic Comparison Profile",
        "email": "comparison-candidate@example.test", "contact": {},
        "preferences": {"desired_roles": [], "avoid_terms": []},
        "facts": [], "answers": [],
    },
    "foreign": {
        "id": "foreign", "name": "Synthetic Foreign Profile",
        "email": "comparison-foreign@example.test", "contact": {},
        "preferences": {"desired_roles": [], "avoid_terms": []},
        "facts": [], "answers": [],
    },
}


def _comparison_recovery_is_mounted() -> bool:
    """Require connected source and bundle markers before Chromium can run."""
    main_source = (ROOT / "apps/web/src/main.tsx").read_text(encoding="utf-8")
    api_sources = [path.read_text(encoding="utf-8") for path in (ROOT / "apps/api").glob("*.py")]
    index = ROOT / "apps/web/dist/index.html"
    bundles = list((ROOT / "apps/web/dist/assets").glob("*.js"))
    api_mounted = any(
        "create_job_comparison_router" in source or "/jobs/compare" in source
        for source in api_sources
    )
    if not index.is_file() or not bundles or "JobComparisonWorkspace" not in main_source or not api_mounted:
        return False
    return any(
        "Compare selected jobs" in bundle.read_text(encoding="utf-8", errors="ignore")
        for bundle in bundles
    )


def _save_search(store, search_id, profile_id, title):
    return save_search_profile(store, {
        "id": search_id, "profile_id": profile_id, "name": search_id,
        "enabled": False, "include_titles": [title], "exclude_titles": [],
        "required_keywords": [], "optional_keywords": [], "excluded_keywords": [],
        "exact_phrases": [], "technology_aliases": {}, "remote": "ANY",
        "employment_types": [], "source_ids": [],
    })


def _snapshot(store):
    """Capture every persisted table to prove comparison remains read-only."""
    with store.engine.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
            for table in m.metadata.sorted_tables
        }


def _insert_match(connection, *, job_id, profile_id, profile_version, score, ref):
    raw_score = min(100.0, max(0.0, float(score) - 8.0))
    connection.execute(m.matches.insert().values(
        id=f"match-{profile_id}-{job_id}", job_id=job_id, profile_id=profile_id,
        profile_version=profile_version, score=score, missing=[],
        components={
            "matched": ["Linux"], "fit_factors": [], "missing": [],
            "scoring_profile": {
                "selection": "ROLE_FAMILY", "profile_id": f"{profile_id}-role-profile",
                "profile_version": "synthetic-score-v1",
                "requested_role_family": "Systems Operations",
                "selected_role_family": "Systems Operations",
            },
            "scoring_contributions": [{
                "dimension": "skills", "weight": 80.0, "raw_score": raw_score,
                "weighted_contribution": round(raw_score * 0.8, 2),
                "explanation": "Synthetic approved skill evidence supports this saved score.",
                "provenance": {"source": "SYNTHETIC_APPROVED_SKILLS", "evidence_ref": ref},
            }],
        },
    ))


@unittest.skipUnless(
    _comparison_recovery_is_mounted(),
    "Root must mount the comparison workspace/API and rebuild apps/web before Chromium acceptance",
)
class JobComparisonScopeRecoveryBrowserTests(unittest.TestCase):
    def test_scope_change_discards_pending_result_and_retry_uses_new_order_without_writes(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-job-comparison-recovery-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'comparison-recovery.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            for profile in PROFILES.values():
                store.save_profile(deepcopy(profile))
            _save_search(store, "cloud-search", "candidate", "Cloud")
            _save_search(store, "foreign-search", "foreign", "Foreign")
            candidate_version = store.profile("candidate")["version"]
            foreign_version = store.profile("foreign")["version"]

            jobs = (
                ("cloud-first", "Cloud Systems Engineer", "candidate", candidate_version, 82.0),
                ("cloud-second", "Cloud Platform Engineer", "candidate", candidate_version, 76.0),
                ("foreign-first", "Foreign Systems Engineer", "foreign", foreign_version, 95.0),
                ("foreign-second", "Foreign Platform Engineer", "foreign", foreign_version, 90.0),
            )
            with store.engine.begin() as connection:
                connection.execute(m.companies.insert().values(
                    id="synthetic-comparison-company", name="Synthetic Comparison Co.",
                    domain="comparison-recovery.example.test",
                ))
                for job_id, title, profile_id, profile_version, score in jobs:
                    connection.execute(m.jobs.insert().values(
                        id=job_id, company_id="synthetic-comparison-company",
                        requisition=f"fixture-{job_id}", title=title,
                        description=f"Synthetic {title} role requiring Linux.",
                        skills=["Linux"], location="Remote", remote_type="REMOTE",
                        attributes={"fictional": True},
                    ))
                    connection.execute(m.routes.insert().values(
                        id=f"route-{job_id}", job_id=job_id,
                        company_id="synthetic-comparison-company", provider="fixture",
                        tenant="job-comparison-recovery-fixture", requisition=f"fixture-{job_id}",
                        url=f"https://jobs.example.test/apply/{job_id}",
                    ))
                    _insert_match(
                        connection, job_id=job_id, profile_id=profile_id,
                        profile_version=profile_version, score=score,
                        ref=f"fixture:{job_id}:skills",
                    )

            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            comparison_requests = []
            held_requests = []
            external_requests = []
            blocked_writes = []
            submission_requests = []
            ai_requests = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated comparison API did not start")
                    time.sleep(0.02)
                database_before_browser = _snapshot(store)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_read_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        path = parsed.path.lower()
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif any(token in path for token in (
                            "/submit", "/mock-run", "/applications/submit", "/apply",
                        )):
                            submission_requests.append((request.method, request.url))
                            route.abort()
                        elif any(token in path for token in (
                            "/inference", "/generate", "/completions", "/chat/", "/ai/",
                        )):
                            ai_requests.append((request.method, request.url))
                            route.abort()
                        elif path.endswith("/jobs/compare") and request.method == "POST":
                            body = json.loads(request.post_data or "{}")
                            comparison_requests.append({"path": path, "body": body})
                            if len(comparison_requests) == 1:
                                held_requests.append(route)
                                return
                            if len(comparison_requests) == 2:
                                route.fulfill(
                                    status=503,
                                    content_type="application/json",
                                    body=json.dumps({"detail": "SYNTHETIC_PRIVATE_RETRY_FIXTURE"}),
                                )
                                return
                            route.continue_()
                        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                            blocked_writes.append((request.method, request.url))
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_read_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'candidate')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("candidate")

                    search = page.get_by_label("Saved search profile", exact=True)
                    expect(search.locator('option[value="cloud-search"]')).to_have_count(1)
                    search.select_option("cloud-search")
                    expect(page.locator(".job-card").filter(has_text="Cloud Systems Engineer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Cloud Platform Engineer")).to_be_visible()

                    page.get_by_role("checkbox", name="Compare Cloud Platform Engineer", exact=True).check()
                    page.get_by_role("checkbox", name="Compare Cloud Systems Engineer", exact=True).check()
                    page.get_by_role("button", name="Compare selected jobs", exact=True).click()
                    workspace = page.get_by_role("region", name="Job comparison", exact=True)
                    expect(workspace).to_contain_text("Comparing selected jobs")
                    self.assertEqual(len(held_requests), 1)

                    # Changing profile and then search invalidates the pending scope.
                    page.get_by_label("Active profile", exact=True).select_option("foreign")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("foreign")
                    expect(search.locator('option[value="foreign-search"]')).to_have_count(1)
                    search.select_option("foreign-search")
                    expect(page.locator(".job-card").filter(has_text="Foreign Systems Engineer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Foreign Platform Engineer")).to_be_visible()
                    with page.expect_response(
                        lambda response: urlsplit(response.url).path.endswith("/cloud-search/jobs/compare"),
                    ) as old_scope_response:
                        held_requests[0].continue_()
                    self.assertEqual(old_scope_response.value.status, 200)
                    expect(workspace).not_to_contain_text("Cloud Systems Engineer")
                    expect(workspace).not_to_contain_text("cloud-first:skills")

                    # The first request in the new scope fails with private server detail.
                    page.get_by_role("checkbox", name="Compare Foreign Platform Engineer", exact=True).check()
                    page.get_by_role("checkbox", name="Compare Foreign Systems Engineer", exact=True).check()
                    compare = page.get_by_role("button", name="Compare selected jobs", exact=True)
                    expect(compare).to_be_enabled()
                    compare.click()
                    expect(page.get_by_role("button", name="Retry comparison", exact=True)).to_be_visible()
                    expect(workspace).not_to_contain_text("SYNTHETIC_PRIVATE_RETRY_FIXTURE")
                    expect(workspace).not_to_contain_text("Cloud Systems Engineer")

                    # Explicit retry uses the exact selected order and returns scoped evidence.
                    with page.expect_response(
                        lambda response: urlsplit(response.url).path.endswith("/foreign-search/jobs/compare"),
                    ) as retry_response:
                        page.get_by_role("button", name="Retry comparison", exact=True).click()
                    self.assertEqual(retry_response.value.status, 200)
                    expect(workspace).to_contain_text("Foreign Platform Engineer")
                    expect(workspace).to_contain_text("Foreign Systems Engineer")
                    expect(workspace).not_to_contain_text("SYNTHETIC_PRIVATE_RETRY_FIXTURE")
                    expect(workspace).not_to_contain_text("Cloud Systems Engineer")

                    expected_old_ids = ["cloud-second", "cloud-first"]
                    expected_new_ids = ["foreign-second", "foreign-first"]
                    self.assertEqual(
                        [row["body"].get("job_ids") for row in comparison_requests],
                        [expected_old_ids, expected_new_ids, expected_new_ids],
                    )
                    self.assertTrue(all(
                        row["path"].startswith((
                            "/profiles/candidate/search-profiles/",
                            "/profiles/foreign/search-profiles/",
                        ))
                        for row in comparison_requests
                    ))
                    self.assertEqual([row["path"].split("/")[2] for row in comparison_requests],
                                     ["candidate", "foreign", "foreign"])
                    self.assertEqual([row["path"].split("/")[4] for row in comparison_requests],
                                     ["cloud-search", "foreign-search", "foreign-search"])
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(external_requests, [])
                    self.assertEqual(ai_requests, [])
                    self.assertEqual(submission_requests, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated comparison API did not stop")
            self.assertEqual(database_before_browser, _snapshot(store))


if __name__ == "__main__":
    unittest.main()
