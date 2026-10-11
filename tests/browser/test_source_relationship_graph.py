"""Connected Chromium acceptance for the profile-scoped source relationship graph."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SourceRelationshipGraphBrowserTests(unittest.TestCase):
    def test_sources_relationship_selection_refresh_and_profile_readback(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-graph-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'source-graph.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            store.save_profile({**deepcopy(PROFILE), "id": "other",
                                "name": "Other Synthetic Profile",
                                "email": "other@example.test"})
            self._seed_graph(store)

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
            page_errors = []

            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated source-graph API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 1000})
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    response = page.goto(origin, wait_until="domcontentloaded")
                    self.assertIsNotNone(response)
                    self.assertEqual(response.status, 200)
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    page.get_by_role("button", name="Sources", exact=True).click()
                    graph = page.get_by_role("region", name="Source relationship graph")
                    expect(graph).to_be_visible()
                    expect(graph).to_contain_text("Synthetic Graph Employer")
                    expect(graph).not_to_contain_text("Other Synthetic Employer")

                    relation = graph.get_by_role(
                        "button",
                        name="Company → employer source: Synthetic Graph Employer → greenhouse · graph-fixture",
                        exact=True,
                    )
                    expect(relation).to_be_visible()
                    relation.click()
                    selected = graph.get_by_role("complementary", name="Selected graph item")
                    expect(selected).to_contain_text("Company → employer source")
                    expect(selected).to_contain_text("Synthetic Graph Employer → greenhouse · graph-fixture")

                    with page.expect_response(lambda value:
                            value.request.method == "GET" and
                            urlsplit(value.url).path == "/profiles/fixture/source-relationship-graph") as refreshed:
                        graph.get_by_role("button", name="Refresh graph", exact=True).click()
                    self.assertEqual(refreshed.value.status, 200, refreshed.value.text())
                    payload = refreshed.value.json()
                    self.assertEqual(payload["profile_id"], "fixture")
                    self.assertIn("company:graph-company", {node["id"] for node in payload["nodes"]})
                    self.assertNotIn("company:other-company", {node["id"] for node in payload["nodes"]})
                    self.assertIn("Synthetic Graph Employer", [node["label"] for node in payload["nodes"]])
                    expect(graph.get_by_role("button", name="Refresh graph", exact=True)).to_be_enabled()
                    graph.get_by_role(
                        "button",
                        name="Company → employer source: Synthetic Graph Employer → greenhouse · graph-fixture",
                        exact=True,
                    ).click()
                    expect(selected).to_contain_text("Company → employer source")
                    expect(selected).to_contain_text("Synthetic Graph Employer → greenhouse · graph-fixture")
                    expect(graph).not_to_contain_text("Other Synthetic Employer")
                    self.assertEqual(page_errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated browser API must stop")

            self.assertEqual(external_requests, [])

    @staticmethod
    def _seed_graph(store):
        with store.engine.begin() as connection:
            for company_id, name in (
                    ("graph-company", "Synthetic Graph Employer"),
                    ("other-company", "Other Synthetic Employer")):
                connection.execute(m.companies.insert().values(
                    id=company_id, name=name, domain=f"{company_id}.example.test"))

            for profile_id, company_id, job_id, tenant in (
                    ("fixture", "graph-company", "graph-job", "graph-fixture"),
                    ("other", "other-company", "other-job", "other-fixture")):
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id=company_id, requisition=f"REQ-{job_id}",
                    title="Synthetic Platform Engineer", description="Synthetic test role",
                    skills=[], location="Remote", remote_type="REMOTE", attributes={}))
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id, profile_id=profile_id,
                    profile_version="v1", score=90, missing=[], components={}))
                connection.execute(m.routes.insert().values(
                    id=f"route-{job_id}", job_id=job_id, company_id=company_id,
                    provider="greenhouse", tenant=tenant,
                    requisition=f"REQ-{job_id}",
                    url=f"https://apply.example.test/{job_id}?token=synthetic-route-secret"))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}", job_id=job_id, source="fixture-feed",
                    source_job_id=f"source-{job_id}",
                    source_url=f"https://feed.example.test/{job_id}?token=synthetic-posting-secret",
                    application_url="https://apply.example.test",
                    raw={"description": "synthetic-raw-secret"},
                    fetched_at="2026-10-11T12:00:00Z"))
                source_id = f"source-{job_id}"
                connection.execute(m.company_sources.insert().values(
                    id=source_id, company_id=company_id, provider="greenhouse",
                    tenant=tenant, endpoint="https://feed.example.test?token=synthetic-provider-secret",
                    discovered_automatically=True))
                connection.execute(m.posting_snapshots.insert().values(
                    id=f"snapshot-{job_id}", posting_id=f"posting-{job_id}",
                    raw={"description": "synthetic-snapshot-secret"}, sha256="a" * 64,
                    retrieved_at="2026-10-11T12:00:00Z"))
                connection.execute(m.source_quality_observations.insert().values(
                    id=f"quality-{job_id}", company_source_id=source_id,
                    observed_at="2026-10-11T12:00:00Z", succeeded=True))
                connection.execute(m.posting_value_candidates.insert().values(
                    id=f"candidate-{job_id}", posting_id=f"posting-{job_id}",
                    posting_snapshot_id=f"snapshot-{job_id}", company_source_id=source_id,
                    quality_observation_id=f"quality-{job_id}",
                    observed_at="2026-10-11T12:00:00Z", normalized={},
                    application_url="https://apply.example.test"))
                connection.execute(m.audit.insert().values(
                    id=f"discovered-{profile_id}", entity_id=profile_id,
                    action="COMPANY_DISCOVERED", payload={"company_id": company_id},
                    timestamp="2026-10-11T12:00:00Z"))
                connection.execute(m.audit.insert().values(
                    id=f"enrolled-{profile_id}", entity_id=profile_id,
                    action="COMPANY_SOURCE_ENROLLED",
                    payload={"company_id": company_id, "company_source_id": source_id},
                    timestamp="2026-10-11T12:00:00Z"))

            connection.execute(m.applications.insert().values(
                id="graph-application", job_id="graph-job", route_id="route-graph-job",
                profile_id="fixture", profile_version="v1", state="DRAFT", target="MOCK",
                fact_ids=["synthetic-fact-id"], trace={"answer": "synthetic-private-answer"},
                document_version=1, confirmation=None,
                created_at="2026-10-11T12:00:00Z", submitted_at=None))


if __name__ == "__main__":
    unittest.main()
