"""Connected browser acceptance for saved-search scoring-profile durability.

The journey creates and edits a profile, reloads it from the saved search,
persists a real match through the deterministic scorer, and explains that
match without changing search eligibility or another search's configuration.
"""

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

_IMPORT_ERROR = None
try:
    from fastapi.testclient import TestClient
    from playwright.sync_api import expect, sync_playwright
    import uvicorn
    from sqlalchemy import select

    from apps.api.main import create_app
    from fixtures.browser_ports import bound_browser_socket
    from packages.persistence import models as m
    from packages.persistence.store import Store
    from services.discovery.matching import recalculate
    from services.discovery.search_profiles import save_search_profile
    from services.discovery.search_semantics import evaluate_search
except ModuleNotFoundError as error:
    _IMPORT_ERROR = error.name
    expect = None
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[2]
PROFILE = {
    "id": "fixture",
    "name": "Synthetic Scoring Reload Profile",
    "email": "scoring-reload@example.test",
    "contact": {},
    "preferences": {"desired_roles": [], "avoid_terms": []},
    "facts": [{
        "id": "approved-linux",
        "kind": "skill",
        "text": "Administered Linux systems in synthetic test environments",
        "source": "synthetic user-confirmed browser fixture",
        "confidence": 100,
        "verified": True,
        "user_approved": True,
    }],
    "answers": [],
}


def _connected_browser_prerequisites() -> bool:
    if _IMPORT_ERROR is not None:
        return False
    main_source = (ROOT / "apps/web/src/main.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    index = ROOT / "apps/web/dist/index.html"
    bundles = list((ROOT / "apps/web/dist/assets").glob("*.js"))
    if ("RoleScoringProfileEditor" not in main_source
            or "MatchingExplanationWorkflow" not in main_source
            or "create_scoring_profiles_router" not in api_source
            or "create_matching_explanations_router" not in api_source
            or not index.is_file() or not bundles):
        return False
    return any(
        "Role-specific scoring profile" in bundle.read_text(encoding="utf-8", errors="ignore")
        and "Saved match explanation" in bundle.read_text(encoding="utf-8", errors="ignore")
        for bundle in bundles
    )


def _skip_reason() -> str:
    if _IMPORT_ERROR is not None:
        return f"Connected browser dependency is unavailable: {_IMPORT_ERROR}"
    return "Scoring editor, explanation route, or current built web bundle is unavailable"


def _save_search(store, search_id, *, title, required, excluded=(), remote="ANY"):
    return save_search_profile(store, {
        "id": search_id,
        "profile_id": "fixture",
        "name": search_id,
        "enabled": False,
        "include_titles": [title],
        "exclude_titles": list(excluded),
        "required_keywords": [required],
        "optional_keywords": [],
        "excluded_keywords": [],
        "exact_phrases": [],
        "technology_aliases": {},
        "remote": remote,
        "employment_types": [],
        "source_ids": [],
    })


def _get_searches(store):
    with store.engine.connect() as connection:
        return {
            row["id"]: dict(row)
            for row in connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id.in_(("selected-search", "other-search")),
            )).mappings()
        }


def _jobs(store):
    with store.engine.connect() as connection:
        return {
            row["id"]: dict(row)
            for row in connection.execute(select(m.jobs).where(
                m.jobs.c.id.in_(("selected-job", "excluded-job", "other-job")),
            )).mappings()
        }


def _without_scoring_profile(criteria):
    return {key: value for key, value in criteria.items() if key != "role_scoring_profile"}


def _eligibility(criteria_by_search, jobs):
    return {
        search_id: {
            job_id: evaluate_search(criteria, jobs[job_id])["matched"]
            for job_id in jobs
        }
        for search_id, criteria in criteria_by_search.items()
    }


def _scoring_profile(client, search_id, *, role_family, skills, ai_intensity):
    response = client.put(
        f"/profiles/fixture/search-profiles/{search_id}/role-scoring-profile",
        json={
            "expected_version": None,
            "profile": {
                "schema_version": 1,
                "role_family": role_family,
                "weights": {"skills": skills, "ai_intensity": ai_intensity},
                "thresholds": {},
            },
        },
    )
    if response.status_code != 200:
        raise AssertionError(f"Could not seed other-search scoring profile: {response.text}")
    return response.json()["profile"]


@unittest.skipUnless(
    _connected_browser_prerequisites(),
    _skip_reason(),
)
class RoleScoringProfileReloadMatchBrowserTests(unittest.TestCase):
    def test_reload_explains_real_match_without_changing_other_search_or_eligibility(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-role-scoring-reload-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'scoring.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            _save_search(
                store, "selected-search", title="Cloud Systems Engineer",
                required="Linux", excluded=("Temporary Contract",), remote="REQUIRED",
            )
            _save_search(
                store, "other-search", title="Network Operations Engineer",
                required="Routing", remote="ANY",
            )

            with store.engine.begin() as connection:
                connection.execute(m.companies.insert().values(
                    id="synthetic-company", name="Synthetic Infrastructure Co.",
                    domain="scoring-reload.example.test",
                ))
                connection.execute(m.jobs.insert().values(
                    id="selected-job", company_id="synthetic-company",
                    requisition="fixture-cloud", title="Cloud Systems Engineer",
                    description="Linux systems operations with source-reported AI tooling.",
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
                    id="excluded-job", company_id="synthetic-company",
                    requisition="fixture-temp", title="Temporary Contract Cloud Systems Engineer",
                    description="Linux systems operations", skills=["Linux"],
                    location="Remote", remote_type="REMOTE", attributes={},
                ))
                connection.execute(m.jobs.insert().values(
                    id="other-job", company_id="synthetic-company",
                    requisition="fixture-network", title="Network Operations Engineer",
                    description="Routing and network operations", skills=["Routing"],
                    location="Remote", remote_type="REMOTE", attributes={
                        "specialized_signals": {"ai_intensity": {
                            "score": 85,
                            "provenance": {
                                "source": "SYNTHETIC_NETWORK_METADATA",
                                "evidence_ref": "fixture:network-ai",
                            },
                        }},
                    },
                ))
                for job_id, requisition in (
                    ("selected-job", "fixture-cloud"),
                    ("excluded-job", "fixture-temp"),
                    ("other-job", "fixture-network"),
                ):
                    connection.execute(m.routes.insert().values(
                        id=f"route-{job_id}", job_id=job_id,
                        company_id="synthetic-company", provider="GREENHOUSE",
                        tenant="synthetic-scoring-reload", requisition=requisition,
                        url=f"https://scoring-reload.example.test/apply/{job_id}",
                    ))

            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            with TestClient(app) as client:
                other_profile = _scoring_profile(
                    client, "other-search", role_family="Network Operations",
                    skills=25, ai_intensity=75,
                )

            profile_before = store.profile("fixture")
            searches_before = _get_searches(store)
            jobs_before = _jobs(store)
            criteria_before = {
                search_id: deepcopy(row["criteria"])
                for search_id, row in searches_before.items()
            }
            eligibility_before = _eligibility(criteria_before, jobs_before)
            self.assertTrue(eligibility_before["selected-search"]["selected-job"])
            self.assertFalse(eligibility_before["selected-search"]["excluded-job"])
            self.assertTrue(eligibility_before["other-search"]["other-job"])

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            profile_reads = []
            profile_writes = []
            explanation_reads = []
            blocked_writes = []
            external_requests = []
            ai_requests = []
            submission_requests = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated scoring-profile API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def browser_boundary(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        path = parsed.path.lower()
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif any(token in path for token in (
                            "/inference", "/generate", "/completions", "/chat/", "/ai/",
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

                    context.route("**/*", browser_boundary)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    search = page.get_by_label("Saved search profile", exact=True)
                    with page.expect_response(lambda response:
                        response.request.method == "GET"
                        and urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    ):
                        search.select_option("selected-search")

                    editor = page.locator("section").filter(has=page.get_by_role(
                        "heading", name="Role-specific scoring profile", exact=True))
                    expect(editor).to_be_visible()
                    expect(editor).to_contain_text(
                        "No scoring profile is saved for this search yet.")
                    expect(editor).to_contain_text("do not change hard eligibility")
                    editor.get_by_label("Role family", exact=True).fill("Cloud Operations")
                    editor.get_by_label("Enable Skills", exact=True).check()
                    editor.get_by_label("Enable AI intensity", exact=True).check()
                    editor.get_by_label("Skills weight", exact=True).fill("70")
                    editor.get_by_label("AI intensity weight", exact=True).fill("30")
                    with page.expect_response(lambda response:
                        response.request.method == "GET"
                        and urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    ) as create_readback:
                        editor.get_by_role("button", name="Save profile", exact=True).click()
                    created = create_readback.value.json()["profile"]
                    self.assertEqual(create_readback.value.status, 200)
                    self.assertEqual(created["role_family"], "Cloud Operations")
                    self.assertEqual(created["weights"]["skills"], 70)
                    self.assertEqual(created["weights"]["ai_intensity"], 30)

                    page.reload(wait_until="domcontentloaded")
                    search = page.get_by_label("Saved search profile", exact=True)
                    with page.expect_response(lambda response:
                        response.request.method == "GET"
                        and urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    ):
                        search.select_option("selected-search")
                    editor = page.locator("section").filter(has=page.get_by_role(
                        "heading", name="Role-specific scoring profile", exact=True))
                    expect(editor.get_by_label("Role family", exact=True)).to_have_value(
                        "Cloud Operations")
                    expect(editor).to_contain_text(f"current version {created['version']}")

                    editor.get_by_label("Role family", exact=True).fill("Cloud Platform Operations")
                    editor.get_by_label("Skills weight", exact=True).fill("60")
                    editor.get_by_label("AI intensity weight", exact=True).fill("40")
                    with page.expect_response(lambda response:
                        response.request.method == "GET"
                        and urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    ) as edit_readback:
                        with page.expect_request(lambda request:
                            request.method == "PUT"
                            and urlsplit(request.url).path ==
                            "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                        ) as edit_request:
                            editor.get_by_role("button", name="Save profile", exact=True).click()
                    edited = edit_readback.value.json()["profile"]
                    self.assertEqual(edit_readback.value.status, 200)
                    posted = edit_request.value.post_data_json
                    self.assertEqual(posted["expected_version"], created["version"])
                    self.assertEqual(edited["role_family"], "Cloud Platform Operations")
                    self.assertEqual(edited["weights"]["skills"], 60)
                    self.assertEqual(edited["weights"]["ai_intensity"], 40)
                    self.assertNotEqual(edited["version"], created["version"])

                    # Reload after editing, then run the actual deterministic scorer
                    # over the selected persisted job using this search's durable criteria.
                    page.reload(wait_until="domcontentloaded")
                    search = page.get_by_label("Saved search profile", exact=True)
                    with page.expect_response(lambda response:
                        response.request.method == "GET"
                        and urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    ):
                        search.select_option("selected-search")
                    editor = page.locator("section").filter(has=page.get_by_role(
                        "heading", name="Role-specific scoring profile", exact=True))
                    expect(editor.get_by_label("Role family", exact=True)).to_have_value(
                        "Cloud Platform Operations")
                    expect(editor).to_contain_text(f"current version {edited['version']}")

                    selected_search = _get_searches(store)["selected-search"]
                    recalculate(
                        store, "fixture", profile_before["version"],
                        candidate_job_ids=["selected-job"],
                        scoring_criteria=selected_search["criteria"],
                    )
                    with store.engine.connect() as connection:
                        match = dict(connection.execute(select(m.matches).where(
                            m.matches.c.job_id == "selected-job",
                            m.matches.c.profile_id == "fixture",
                            m.matches.c.profile_version == profile_before["version"],
                        )).mappings().one())
                    self.assertEqual(
                        match["components"]["scoring_profile"]["profile_version"],
                        edited["version"],
                    )

                    page.reload(wait_until="domcontentloaded")
                    search = page.get_by_label("Saved search profile", exact=True)
                    with page.expect_response(lambda response:
                        response.request.method == "GET"
                        and urlsplit(response.url).path == "/jobs"
                    ) as jobs_response:
                        search.select_option("selected-search")
                    visible_job_ids = [
                        row["id"] for row in jobs_response.value.json()["jobs"]
                    ]
                    self.assertIn("selected-job", visible_job_ids)
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path.endswith(
                            "/selected-search/jobs/selected-job/match-explanation")
                    ) as explanation_response:
                        page.locator(".job-card").filter(
                            has_text="Cloud Systems Engineer").click()
                    explanation_body = explanation_response.value.json()
                    self.assertEqual(explanation_response.value.status, 200)
                    self.assertEqual(explanation_body["search_profile_id"], "selected-search")
                    self.assertEqual(explanation_body["job_id"], "selected-job")
                    self.assertEqual(explanation_body["scoring_profile"], {
                        "id": edited["id"],
                        "version": edited["version"],
                        "role_family": "Cloud Platform Operations",
                    })
                    explanation_heading = page.get_by_role(
                        "heading", name="Saved match explanation", exact=True)
                    explanation = page.locator(".job-detail section.panel").filter(
                        has=explanation_heading)
                    expect(explanation).to_contain_text("Cloud Platform Operations")
                    expect(explanation).to_contain_text(edited["version"])
                    expect(explanation).to_contain_text("SYNTHETIC_POSTING_METADATA")
                    expect(explanation).to_contain_text("fixture:cloud-ai")
                    contributions = explanation.get_by_role(
                        "table", name="Scoring signal contributions", exact=True)
                    expect(contributions.get_by_role("row").filter(
                        has_text="AI intensity")).to_contain_text("35")

                    search.select_option("other-search")
                    expect(editor.get_by_label("Role family", exact=True)).to_have_value(
                        "Network Operations")
                    self.assertEqual(other_profile["role_family"], "Network Operations")

                    searches_after = _get_searches(store)
                    criteria_after = {
                        search_id: row["criteria"]
                        for search_id, row in searches_after.items()
                    }
                    for search_id in ("selected-search", "other-search"):
                        self.assertEqual(
                            _without_scoring_profile(criteria_after[search_id]),
                            _without_scoring_profile(criteria_before[search_id]),
                            f"Scoring edits changed hard/search criteria for {search_id}",
                        )
                    self.assertEqual(
                        criteria_after["other-search"]["role_scoring_profile"],
                        criteria_before["other-search"]["role_scoring_profile"],
                    )
                    self.assertEqual(_eligibility(criteria_after, _jobs(store)), eligibility_before)
                    self.assertEqual(store.profile("fixture"), profile_before)
                    self.assertEqual(len(profile_writes), 2)
                    self.assertGreaterEqual(len(profile_reads), 5)
                    self.assertGreaterEqual(len(explanation_reads), 1)
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(external_requests, [])
                    self.assertEqual(ai_requests, [])
                    self.assertEqual(submission_requests, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated scoring-profile API did not stop")


if __name__ == "__main__":
    unittest.main()
