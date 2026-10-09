"""Connected browser proof that editor readback and match detail share search scope.

The role-specific explanation workflow is mounted by the integration lane. Until
the UI and rebuilt bundle are present, skip rather than run against stale assets.
The deterministic selected-search scorer is covered by the integration flow test;
this browser acceptance seeds its local persisted result after the editor save.
"""

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import build_explanation
from services.discovery.search_profiles import save_search_profile
from services.matching.profile_scoring import compose_profile_scoring
from services.matching.scoring_profiles import resolve_role_scoring_profile


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PROFILE = {
    "id": "fixture",
    "name": "Synthetic Role Scoring Browser Profile",
    "email": "role-scoring-browser@example.test",
    "contact": {},
    "preferences": {"desired_roles": [], "avoid_terms": []},
    "facts": [],
    "answers": [],
}


def _connected_workflow_bundle_exists() -> bool:
    """Avoid Chromium acceptance against an unmounted API/UI or stale web build."""
    main_source = (ROOT / "apps/web/src/main.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    index = ROOT / "apps/web/dist/index.html"
    bundles = list((ROOT / "apps/web/dist/assets").glob("*.js"))
    if not index.is_file() or not bundles:
        return False
    if "RoleScoringProfileEditor" not in main_source:
        return False
    if "MatchingExplanationWorkflow" not in main_source:
        return False
    if "create_scoring_profiles_router" not in api_source:
        return False
    if "create_matching_explanations_router" not in api_source:
        return False
    return any(
        "Role-specific scoring profile" in bundle.read_text(encoding="utf-8", errors="ignore")
        and "Saved match explanation" in bundle.read_text(encoding="utf-8", errors="ignore")
        for bundle in bundles
    )


def _save_search(store, search_id, *, title, keyword):
    return save_search_profile(store, {
        "id": search_id,
        "profile_id": "fixture",
        "name": search_id,
        "enabled": False,
        "include_titles": [title],
        "exclude_titles": [],
        "required_keywords": [keyword],
        "optional_keywords": [],
        "excluded_keywords": [],
        "exact_phrases": [],
        "technology_aliases": {},
        "remote": "ANY",
        "employment_types": [],
        "source_ids": [],
    })


def _set_scoring_profile(client, search_id, role_family, weights):
    response = client.put(
        f"/profiles/fixture/search-profiles/{search_id}/role-scoring-profile",
        json={
            "expected_version": None,
            "profile": {
                "schema_version": 1,
                "role_family": role_family,
                "weights": weights,
                "thresholds": {},
            },
        },
    )
    if response.status_code != 200:
        raise AssertionError(f"Could not seed synthetic scoring profile: {response.text}")
    return response.json()["profile"]


def _materialize_selected_search_match(store, *, search_id, job_id, career_profile,
                                       scoring_profile):
    """Seed the deterministic scorer's persisted result at the consumer seam."""
    with store.engine.connect() as connection:
        job = dict(connection.execute(select(m.jobs).where(
            m.jobs.c.id == job_id,
        )).mappings().one())
    legacy = build_explanation(job, career_profile)
    resolution = resolve_role_scoring_profile(
        scoring_profile["role_family"],
        {scoring_profile["role_family"]: scoring_profile},
    )
    composed = compose_profile_scoring(job, legacy, resolution)
    components = {
        **legacy["components"],
        "scoring_profile": composed["scoring_profile"],
        "scoring_contributions": composed["scoring_contributions"],
    }
    with store.engine.begin() as connection:
        connection.execute(m.matches.insert().values(
            id=f"match-{search_id}-{job_id}",
            job_id=job_id,
            profile_id="fixture",
            profile_version=career_profile["version"],
            score=composed["score"],
            missing=legacy["missing"],
            components=components,
        ))
    return composed


@unittest.skipUnless(
    _connected_workflow_bundle_exists(),
    "Root must mount both profile editor and match explanation workflow and rebuild apps/web",
)
class RoleScoringSelectedSearchExplanationBrowserTests(unittest.TestCase):
    def test_editor_readback_and_job_explanation_follow_the_selected_saved_search(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-selected-search-explanation-browser-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(FIXTURE_PROFILE))
            _save_search(
                store, "selected-search", title="Cloud Systems Engineer", keyword="Linux",
            )
            _save_search(
                store, "other-search", title="Network Operations Engineer", keyword="Routing",
            )
            with store.engine.begin() as connection:
                connection.execute(m.companies.insert().values(
                    id="synthetic-company", name="Synthetic Infrastructure Co.",
                    domain="synthetic.example.test",
                ))
                connection.execute(m.jobs.insert().values(
                    id="selected-job", company_id="synthetic-company", requisition="fixture-cloud",
                    title="Cloud Systems Engineer", description="Linux systems operations",
                    skills=["Linux"], location="Remote", remote_type="REMOTE",
                    attributes={"specialized_signals": {"ai_intensity": {
                        "score": 35,
                        "provenance": {
                            "source": "SYNTHETIC_POSTING_METADATA",
                            "evidence_ref": "fixture:cloud-ai",
                        },
                    }}},
                ))
                connection.execute(m.jobs.insert().values(
                    id="other-job", company_id="synthetic-company", requisition="fixture-network",
                    title="Network Operations Engineer", description="Routing and network operations",
                    skills=["Routing"], location="Remote", remote_type="REMOTE",
                    attributes={"specialized_signals": {"ai_intensity": {
                        "score": 85,
                        "provenance": {
                            "source": "SYNTHETIC_NETWORK_METADATA",
                            "evidence_ref": "fixture:network-ai",
                        },
                    }}},
                ))

                for job_id, requisition in (
                    ("selected-job", "fixture-cloud"),
                    ("other-job", "fixture-network"),
                ):
                    connection.execute(m.routes.insert().values(
                        id=f"route-{job_id}", job_id=job_id,
                        company_id="synthetic-company", provider="GREENHOUSE",
                        tenant="synthetic-scoring-explanation", requisition=requisition,
                        url=f"https://scoring-explanation.example.test/apply/{job_id}",
                    ))

            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            with TestClient(app) as client:
                initial_selected = _set_scoring_profile(
                    client, "selected-search", "Cloud Operations",
                    {"skills": 70, "ai_intensity": 30, "education": 10},
                )
                other_saved = _set_scoring_profile(
                    client, "other-search", "Network Operations", {"skills": 25, "ai_intensity": 75},
                )
                career_profile = store.profile("fixture")
                _materialize_selected_search_match(
                    store, search_id="other-search", job_id="other-job",
                    career_profile=career_profile, scoring_profile=other_saved,
                )

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            external_requests = []
            ai_requests = []
            submission_requests = []
            blocked_writes = []
            profile_reads = []
            profile_writes = []
            explanation_reads = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated scoring-profile browser API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_scoring_read_and_profile_write_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        path = parsed.path.lower()
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif any(token in path for token in (
                            "/inference", "/generate", "/completions", "/chat/",
                        )):
                            ai_requests.append((request.method, request.url))
                            route.abort()
                        elif any(token in path for token in (
                            "/submit", "/mock-run", "/applications/submit", "/apply",
                        )):
                            submission_requests.append((request.method, request.url))
                            route.abort()
                        elif path.endswith("/role-scoring-profile"):
                            if request.method == "GET":
                                profile_reads.append(request.url)
                                route.continue_()
                            elif request.method == "PUT":
                                profile_writes.append(request)
                                route.continue_()
                            else:
                                blocked_writes.append((request.method, request.url))
                                route.abort()
                        elif path.endswith("/match-explanation") and request.method == "GET":
                            explanation_reads.append(request.url)
                            route.continue_()
                        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                            blocked_writes.append((request.method, request.url))
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_scoring_read_and_profile_write_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    saved_search = page.get_by_label("Saved search profile", exact=True)
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                        and response.request.method == "GET"
                    ) as initial_profile_read:
                        saved_search.select_option("selected-search")
                    self.assertEqual(initial_profile_read.value.status, 200)

                    editor = page.locator("section").filter(has=page.get_by_role(
                        "heading", name="Role-specific scoring profile", exact=True))
                    expect(editor).to_be_visible()
                    expect(editor.get_by_label("Role family", exact=True)).to_have_value(
                        "Cloud Operations")
                    expect(editor).to_contain_text(f"current version {initial_selected['version']}")

                    editor.get_by_label("Role family", exact=True).fill("Cloud Platform Operations")
                    editor.get_by_label("Skills weight", exact=True).fill("60")
                    editor.get_by_label("AI intensity weight", exact=True).fill("40")
                    profile_path = (
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    )
                    with page.expect_response(lambda response:
                        response.request.method == "GET"
                        and urlsplit(response.url).path == profile_path
                    ) as editor_readback:
                        with page.expect_request(lambda request:
                            request.method == "PUT"
                            and urlsplit(request.url).path == profile_path
                        ) as save_request:
                            editor.get_by_role("button", name="Save profile", exact=True).click()
                    posted = save_request.value.post_data_json
                    self.assertEqual(posted["expected_version"], initial_selected["version"])
                    self.assertEqual(posted["profile"]["role_family"], "Cloud Platform Operations")
                    self.assertEqual(posted["profile"]["weights"]["skills"], 60)
                    self.assertEqual(posted["profile"]["weights"]["ai_intensity"], 40)
                    durable = editor_readback.value.json()["profile"]
                    self.assertEqual(editor_readback.value.status, 200)
                    self.assertEqual(durable["role_family"], "Cloud Platform Operations")
                    self.assertEqual(durable["weights"]["skills"], 60)
                    self.assertEqual(durable["weights"]["ai_intensity"], 40)
                    self.assertNotEqual(durable["version"], initial_selected["version"])
                    expect(editor).to_contain_text(f"current version {durable['version']}")

                    # The selected-search scoring consumer is covered by the integration oracle.
                    # Materialize the same deterministic result after save so this browser test
                    # proves the mounted detail workflow binds to the durable selected profile.
                    selected_result = _materialize_selected_search_match(
                        store, search_id="selected-search", job_id="selected-job",
                        career_profile=career_profile, scoring_profile=durable,
                    )
                    self.assertEqual(selected_result["scoring_profile"]["profile_version"],
                                     durable["version"])

                    saved_search.select_option("other-search")
                    expect(editor.get_by_label("Role family", exact=True)).to_have_value(
                        "Network Operations")
                    saved_search.select_option("selected-search")
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/jobs/selected-job/match-explanation"
                    ) as selected_explanation:
                        page.locator(".job-card").filter(
                            has_text="Cloud Systems Engineer").click()
                    selected_body = selected_explanation.value.json()
                    self.assertEqual(selected_body["profile_id"], "fixture")
                    self.assertEqual(selected_body["search_profile_id"], "selected-search")
                    self.assertEqual(selected_body["job_id"], "selected-job")
                    self.assertEqual(selected_body["scoring_profile"], {
                        "id": durable["id"],
                        "version": durable["version"],
                        "role_family": "Cloud Platform Operations",
                    })
                    expect(page.get_by_role("heading", name="Saved match explanation")).to_be_visible()
                    explanation = page.locator(".job-detail section.panel").filter(has=page.get_by_role(
                        "heading", name="Saved match explanation", exact=True))
                    expect(explanation).to_contain_text(durable["version"])
                    expect(explanation).to_contain_text("Cloud Platform Operations")
                    expect(explanation).to_contain_text("SYNTHETIC_POSTING_METADATA")
                    expect(explanation).to_contain_text("fixture:cloud-ai")
                    expect(explanation).to_contain_text("UNKNOWN")
                    contribution_table = explanation.get_by_role(
                        "table", name="Scoring signal contributions", exact=True)
                    expect(contribution_table.get_by_role("row").filter(
                        has_text="AI intensity")).to_contain_text("35")
                    expect(contribution_table.get_by_role("row").filter(
                        has_text="Education")).to_contain_text("UNKNOWN")

                    saved_search.select_option("other-search")
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/other-search/jobs/other-job/match-explanation"
                    ) as other_explanation:
                        page.locator(".job-card").filter(
                            has_text="Network Operations Engineer").click()
                    other_body = other_explanation.value.json()
                    self.assertEqual(other_body["search_profile_id"], "other-search")
                    self.assertEqual(other_body["job_id"], "other-job")
                    self.assertEqual(other_body["scoring_profile"]["id"], other_saved["id"])
                    self.assertEqual(other_body["scoring_profile"]["version"], other_saved["version"])
                    expect(explanation).to_contain_text("Network Operations")
                    expect(explanation).to_contain_text("fixture:network-ai")
                    expect(explanation).not_to_contain_text("Cloud Platform Operations")
                    expect(explanation).not_to_contain_text("fixture:cloud-ai")

                    self.assertEqual(len(profile_writes), 1)
                    self.assertGreaterEqual(len(profile_reads), 3)
                    self.assertGreaterEqual(len(explanation_reads), 2)
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
            self.assertFalse(thread.is_alive(), "Isolated scoring-profile API did not stop")


if __name__ == "__main__":
    unittest.main()
