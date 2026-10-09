"""Connected browser acceptance for scoped multi-job comparison.

The app does not mount a job-comparison workspace yet. Keep this test skipped
until the selected-job controls and comparison view are present in the built UI.
"""

from copy import deepcopy
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


def _comparison_workspace_is_mounted() -> bool:
    """Require the actual mounted component and rebuilt bundle, not a mock."""
    main_source = (ROOT / "apps/web/src/main.tsx").read_text(encoding="utf-8")
    index = ROOT / "apps/web/dist/index.html"
    bundles = list((ROOT / "apps/web/dist/assets").glob("*.js"))
    if not index.is_file() or not bundles or "JobComparisonWorkspace" not in main_source:
        return False
    return any(
        "Compare selected jobs" in bundle.read_text(encoding="utf-8", errors="ignore")
        and "Shared observed requirements" in bundle.read_text(encoding="utf-8", errors="ignore")
        for bundle in bundles
    )


def _save_search(store):
    return save_search_profile(store, {
        "id": "candidate-search", "profile_id": "candidate", "name": "Cloud comparison",
        "enabled": False, "include_titles": ["Cloud"], "exclude_titles": [],
        "required_keywords": [], "optional_keywords": [], "excluded_keywords": [],
        "exact_phrases": [], "technology_aliases": {}, "remote": "ANY",
        "employment_types": [], "source_ids": [],
    })


def _requirements(*rows):
    return [
        {
            "skill": label,
            "strength": "REQUIRED",
            "weight": 1.0,
            "profile_evidence": "APPROVED",
            "explanation": f"Synthetic posting explicitly lists {label}.",
            "provenance": {
                "source": "SYNTHETIC_JOB_DESCRIPTION",
                "evidence_ref": evidence_ref,
            },
        }
        for label, evidence_ref in rows
    ]


def _insert_match(connection, *, job_id, profile_id, profile_version, score,
                  requirements, ref_prefix, scoring_version):
    skill_raw_score = round((score - 15.0) / 0.8, 2)
    score_rows = [
        {
            "dimension": "skills", "weight": 80.0, "raw_score": skill_raw_score,
            "weighted_contribution": round(skill_raw_score * 0.8, 2),
            "explanation": "Approved skills support this saved match score.",
            "provenance": {
                "source": "APPROVED_PROFILE_SKILLS",
                "evidence_ref": f"{ref_prefix}:approved-skills",
            },
        },
        {
            "dimension": "title", "weight": 20.0, "raw_score": 75.0,
            "weighted_contribution": 15.0,
            "explanation": "The title signal is a comparison aid, not a qualification.",
            "provenance": {
                "source": "SAVED_ROLE_PROFILE",
                "evidence_ref": f"{ref_prefix}:title",
            },
        },
    ]
    connection.execute(m.matches.insert().values(
        id=f"match-{profile_id}-{job_id}", job_id=job_id, profile_id=profile_id,
        profile_version=profile_version, score=score, missing=[],
        components={
            "matched": [row["skill"] for row in requirements],
            "fit_factors": [], "missing": [], "requirements": requirements,
            "scoring_profile": {
                "selection": "ROLE_FAMILY", "profile_id": f"{profile_id}-role-profile",
                "profile_version": scoring_version,
                "requested_role_family": "Cloud Operations",
                "selected_role_family": "Cloud Operations",
            },
            "scoring_contributions": score_rows,
        },
    ))


@unittest.skipUnless(
    _comparison_workspace_is_mounted(),
    "Root must mount JobComparisonWorkspace and rebuild apps/web before Chromium acceptance",
)
class JobComparisonWorkspaceBrowserTests(unittest.TestCase):
    def test_comparison_is_profile_search_scoped_explainable_and_read_only(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-job-comparison-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'comparison.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            for profile in PROFILES.values():
                store.save_profile(deepcopy(profile))
            _save_search(store)
            candidate_version = store.profile("candidate")["version"]
            foreign_version = store.profile("foreign")["version"]

            jobs = (
                ("shared-job", "Cloud Systems Engineer", "Linux AWS operations", ["Linux", "AWS"]),
                ("specific-job", "Cloud Platform Engineer", "Linux Kubernetes operations", ["Linux", "Kubernetes"]),
                ("stale-job", "Cloud Stale Engineer", "Linux legacy operations", ["Linux"]),
                ("foreign-job", "Cloud Foreign Engineer", "Linux foreign operations", ["Linux"]),
            )
            with store.engine.begin() as connection:
                connection.execute(m.companies.insert().values(
                    id="synthetic-comparison-company", name="Synthetic Comparison Co.",
                    domain="comparison.example.test",
                ))
                for job_id, title, description, skills in jobs:
                    requisition = f"comparison-{job_id}"
                    connection.execute(m.jobs.insert().values(
                        id=job_id, company_id="synthetic-comparison-company",
                        requisition=requisition, title=title, description=description,
                        skills=skills, location="Remote", remote_type="REMOTE",
                        attributes={"fictional": True},
                    ))
                    connection.execute(m.routes.insert().values(
                        id=f"route-{job_id}", job_id=job_id,
                        company_id="synthetic-comparison-company", provider="fixture",
                        tenant="job-comparison-fixture", requisition=requisition,
                        url=f"https://jobs.example.test/apply/{job_id}",
                    ))

                first_requirements = _requirements(
                    ("Linux", "shared-job:linux"), ("AWS", "shared-job:aws"),
                )
                second_requirements = _requirements(
                    ("Linux", "specific-job:linux"), ("Kubernetes", "specific-job:kubernetes"),
                )
                _insert_match(
                    connection, job_id="shared-job", profile_id="candidate",
                    profile_version=candidate_version, score=82.0,
                    requirements=first_requirements, ref_prefix="shared-job",
                    scoring_version="candidate-score-v1",
                )
                _insert_match(
                    connection, job_id="specific-job", profile_id="candidate",
                    profile_version=candidate_version, score=74.0,
                    requirements=second_requirements, ref_prefix="specific-job",
                    scoring_version="candidate-score-v1",
                )
                _insert_match(
                    connection, job_id="stale-job", profile_id="candidate",
                    profile_version="stale-career-profile-version", score=91.0,
                    requirements=_requirements(("Linux", "stale-job:linux")),
                    ref_prefix="stale-job", scoring_version="stale-score-version",
                )
                _insert_match(
                    connection, job_id="foreign-job", profile_id="foreign",
                    profile_version=foreign_version, score=95.0,
                    requirements=_requirements(("Linux", "foreign-job:linux")),
                    ref_prefix="foreign-job", scoring_version="foreign-score-v1",
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
            external_requests = []
            blocked_writes = []
            submission_requests = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated comparison API did not start")
                    time.sleep(0.02)

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
                    search.select_option("candidate-search")
                    expect(page.locator(".job-card").filter(
                        has_text="Cloud Systems Engineer")).to_be_visible()
                    expect(page.locator(".job-card").filter(
                        has_text="Cloud Platform Engineer")).to_be_visible()

                    first = page.get_by_role(
                        "checkbox", name="Compare Cloud Systems Engineer", exact=True,
                    )
                    second = page.get_by_role(
                        "checkbox", name="Compare Cloud Platform Engineer", exact=True,
                    )
                    first.check()
                    compare = page.get_by_role(
                        "button", name="Compare selected jobs", exact=True,
                    )
                    expect(compare).to_be_disabled()
                    second.check()
                    expect(compare).to_be_enabled()
                    compare.click()

                    workspace = page.get_by_role("region", name="Job comparison", exact=True)
                    expect(workspace).to_be_visible()
                    expect(workspace).to_contain_text("Cloud Systems Engineer")
                    expect(workspace).to_contain_text("82")
                    expect(workspace).to_contain_text("shared-job:approved-skills")
                    expect(workspace).to_contain_text("Cloud Platform Engineer")
                    expect(workspace).to_contain_text("74")
                    expect(workspace).to_contain_text("specific-job:approved-skills")
                    shared = workspace.get_by_role(
                        "region", name="Shared observed requirements", exact=True,
                    )
                    expect(shared).to_contain_text("Linux")
                    expect(shared).to_contain_text("shared-job:linux")
                    specific = workspace.get_by_role(
                        "region", name="Job-specific observed requirements", exact=True,
                    )
                    expect(specific).to_contain_text("AWS")
                    expect(specific).to_contain_text("shared-job:aws")
                    expect(specific).to_contain_text("Kubernetes")
                    expect(specific).to_contain_text("specific-job:kubernetes")
                    expect(workspace).not_to_contain_text("Cloud Stale Engineer")
                    expect(workspace).not_to_contain_text("Cloud Foreign Engineer")

                    stale_selection = page.get_by_role(
                        "checkbox", name="Compare Cloud Stale Engineer", exact=True,
                    )
                    if stale_selection.count():
                        expect(stale_selection).to_be_disabled()
                    foreign_selection = page.get_by_role(
                        "checkbox", name="Compare Cloud Foreign Engineer", exact=True,
                    )
                    if foreign_selection.count():
                        expect(foreign_selection).to_be_disabled()

                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(external_requests, [])
                    self.assertEqual(submission_requests, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated comparison API did not stop")


if __name__ == "__main__":
    unittest.main()
