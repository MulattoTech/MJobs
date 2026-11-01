"""The named employer fixture remains discoverable, deduplicated and durable."""
import httpx

from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


def test_company_navigation_discovers_two_jobs_preserves_copies_and_reopens(tmp_path):
    url = f"sqlite:///{(tmp_path / 'company.db').as_posix()}"
    store = Store(url)
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    store.save_profile(PROFILE)
    try:
        with fixture_server(tmp_path / "receipts.json") as origin:
            discover(store, origin, "fixture")
            discover(store, origin, "fixture")
            rows = store.jobs("fixture")
            assert {row["requisition"] for row in rows} == {"A", "B"}
            assert {row["company"] for row in rows} == {"Example Systems Inc."}
            assert {row["attributes"]["provider"] for row in rows} == {"mock-ats"}
            assert store.count("company_job_sources") == 1
            assert store.count("job_postings") == 4
            copies = {row["requisition"]: row["posting_count"] for row in rows}
            assert copies == {"A": 3, "B": 1}
            identities = {row["requisition"]: row["id"] for row in rows}
            assert len(set(identities.values())) == 2
            response = httpx.get(origin + "/_test/request-counts", timeout=5, trust_env=False)
            assert response.json() == {"review": 0, "submit": 0}
        store.engine.dispose()
        reopened = Store(url)
        try:
            assert {row["requisition"]: row["id"] for row in reopened.jobs("fixture")} == identities
            assert reopened.count("job_postings") == 4
            assert reopened.count("applications") == 0
        finally:
            reopened.engine.dispose()
    finally:
        store.engine.dispose()
