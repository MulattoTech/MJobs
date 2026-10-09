"""Connected browser acceptance for selected-search role-scoring profiles.

The UI is owned by the root integration lane. This acceptance stays skipped until
the editor is mounted in main.tsx, the profile API is mounted in create_app, and
the web bundle has been rebuilt from those sources.
"""

from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PROFILE = {
    "id": "fixture",
    "name": "Scoring Profile Browser Fixture",
    "email": "scoring-browser@example.test",
    "contact": {},
    "preferences": {"desired_roles": [], "avoid_terms": []},
    "facts": [],
    "answers": [],
}


def _connected_editor_bundle_exists() -> bool:
    """Do not launch Chromium against stale or unmounted frontend/API code."""
    main_source = (ROOT / "apps/web/src/main.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    index = ROOT / "apps/web/dist/index.html"
    bundles = list((ROOT / "apps/web/dist/assets").glob("*.js"))
    if not index.is_file() or not bundles:
        return False
    if "RoleScoringProfileEditor" not in main_source:
        return False
    if "create_scoring_profiles_router" not in api_source:
        return False
    return any("Role-specific scoring profile" in bundle.read_text(
        encoding="utf-8", errors="ignore") for bundle in bundles)


@unittest.skipUnless(
    _connected_editor_bundle_exists(),
    "Root must mount the role-scoring editor/API and rebuild apps/web before Chromium acceptance",
)
class RoleScoringProfileEditorBrowserTests(unittest.TestCase):
    def test_selected_search_profile_edit_readback_and_isolation(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-role-scoring-editor-browser-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(FIXTURE_PROFILE))

            selected_search = save_search_profile(store, {
                "id": "selected-search", "profile_id": "fixture", "name": "Cloud search",
                "enabled": False, "include_titles": ["Systems Administrator"],
                "exclude_titles": ["Junior Helpdesk Technician"],
                "required_keywords": ["Linux"], "optional_keywords": [],
                "excluded_keywords": ["temporary contract"], "exact_phrases": [],
                "technology_aliases": {}, "remote": "REQUIRED",
                "employment_types": ["FULL_TIME"], "source_ids": [],
            })
            second_search = save_search_profile(store, {
                "id": "other-search", "profile_id": "fixture", "name": "Help Desk search",
                "enabled": False, "include_titles": ["Help Desk Analyst"],
                "exclude_titles": [], "required_keywords": ["support"],
                "optional_keywords": [], "excluded_keywords": [], "exact_phrases": [],
                "technology_aliases": {}, "remote": "ANY",
                "employment_types": [], "source_ids": [],
            })
            with store.engine.connect() as connection:
                criteria_before = {
                    row["id"]: deepcopy(row["criteria"])
                    for row in connection.execute(select(m.search_profiles).where(
                        m.search_profiles.c.id.in_(["selected-search", "other-search"]),
                    )).mappings()
                }

            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            with TestClient(app) as client:
                seeded = {}
                for search_id, role_family, skills_weight, skill_threshold in (
                    ("selected-search", "Systems Administration", 55, 60),
                    ("other-search", "Help Desk", 30, 40),
                ):
                    response = client.put(
                        f"/profiles/fixture/search-profiles/{search_id}/role-scoring-profile",
                        json={
                            "expected_version": None,
                            "profile": {
                                "schema_version": 1,
                                "role_family": role_family,
                                "weights": {"skills": skills_weight, "title": 20},
                                "thresholds": {"skills": skill_threshold},
                            },
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    seeded[search_id] = response.json()["profile"]

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            blocked_writes = []
            external_requests = []
            ai_requests = []
            submission_requests = []
            scoring_profile_writes = []
            scoring_profile_reads = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated role-scoring API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_read_and_scoring_write_only(route):
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
                                scoring_profile_reads.append(request.url)
                                route.continue_()
                            elif request.method == "PUT":
                                scoring_profile_writes.append(request)
                                route.continue_()
                            else:
                                blocked_writes.append((request.method, request.url))
                                route.abort()
                        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                            blocked_writes.append((request.method, request.url))
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_read_and_scoring_write_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    saved_search = page.get_by_label(
                        "Search profile for suggested titles", exact=True)
                    with page.expect_response(lambda response:
                        urlsplit(response.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    ):
                        saved_search.select_option("selected-search")

                    editor = page.locator("section").filter(has=page.get_by_role(
                        "heading", name="Role-specific scoring profile", exact=True))
                    expect(editor).to_be_visible()
                    expect(editor).to_contain_text("Advisory scoring only")
                    expect(editor).to_contain_text("do not change hard eligibility")
                    expect(editor.get_by_label("Role family", exact=True)).to_have_value(
                        "Systems Administration")
                    expect(editor.get_by_text(
                        f"current version {seeded['selected-search']['version']}",
                        exact=False,
                    )).to_be_visible()
                    expect(editor.get_by_role("button", name=re.compile(
                        r"submit|apply", re.IGNORECASE))).to_have_count(0)

                    editor.get_by_label("Role family", exact=True).fill("Cloud Operations")
                    editor.get_by_label("Skills weight", exact=True).fill("88")
                    editor.get_by_label("Skills soft threshold", exact=True).fill("73")
                    with page.expect_request(lambda request:
                        request.method == "PUT"
                        and urlsplit(request.url).path ==
                        "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"
                    ) as save_request:
                        editor.get_by_role("button", name="Save profile", exact=True).click()
                    posted = save_request.value.post_data_json
                    self.assertEqual(posted["expected_version"], seeded["selected-search"]["version"])
                    self.assertEqual(posted["profile"]["role_family"], "Cloud Operations")
                    self.assertEqual(posted["profile"]["weights"]["skills"], 88)
                    self.assertEqual(posted["profile"]["thresholds"]["skills"], 73)

                    readback = page.request.get(
                        origin + "/profiles/fixture/search-profiles/selected-search/role-scoring-profile",
                    )
                    self.assertEqual(readback.status, 200)
                    durable = readback.json()["profile"]
                    self.assertEqual(durable["role_family"], "Cloud Operations")
                    self.assertEqual(durable["weights"]["skills"], 88)
                    self.assertEqual(durable["thresholds"]["skills"], 73)
                    self.assertNotEqual(durable["version"], seeded["selected-search"]["version"])
                    expect(editor).to_contain_text(f"current version {durable['version']}")

                    other_read_count = len([
                        value for value in scoring_profile_reads if "other-search" in value
                    ])
                    saved_search.select_option("other-search")
                    expect(editor.get_by_label("Role family", exact=True)).to_have_value("Help Desk")
                    expect(editor.get_by_label("Skills weight", exact=True)).to_have_value("30")
                    expect(editor.get_by_label("Skills soft threshold", exact=True)).to_have_value("40")
                    self.assertGreater(
                        len([value for value in scoring_profile_reads if "other-search" in value]),
                        other_read_count,
                        "Switching saved search must reload that search's scoring profile",
                    )

                    with store.engine.connect() as connection:
                        criteria_after = {
                            row["id"]: row["criteria"]
                            for row in connection.execute(select(m.search_profiles).where(
                                m.search_profiles.c.id.in_(
                                    ["selected-search", "other-search"]),
                            )).mappings()
                        }
                    for search_id in ("selected-search", "other-search"):
                        self.assertEqual(
                            {key: value for key, value in criteria_after[search_id].items()
                             if key != "role_scoring_profile"},
                            criteria_before[search_id],
                            "Scoring edits must preserve saved-search eligibility criteria",
                        )
                    self.assertEqual(len(scoring_profile_writes), 1)
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
            self.assertFalse(thread.is_alive(), "Isolated role-scoring API did not stop")


if __name__ == "__main__":
    unittest.main()
