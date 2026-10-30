"""Opt-in live public-career-site discovery acceptance for MJ-REQ-229.

The only external destination is the fixed allowlisted public career page below.
The in-process API POST is local admission; the production public client/worker
perform GET-only public collection. This test never opens application flows.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from workers.tasks import execute_operation


LIVE_OPT_IN = "MJOBS_ENABLE_LIVE_COMPANY_DISCOVERY_230"
LIVE_COMPANY_NAME = "GitLab"
LIVE_CAREER_URL = "https://about.gitlab.com/jobs/"
LIVE_ALLOWED_HOSTS = frozenset({"about.gitlab.com"})


@pytest.mark.integration
def test_live_company_discovery_persists_first_party_ats_and_current_job_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise one explicitly enabled, bounded real public discovery flow."""
    if os.environ.get(LIVE_OPT_IN) != "1":
        pytest.skip(f"set {LIVE_OPT_IN}=1 to authorize the single allowlisted live GET flow")

    target = urlsplit(LIVE_CAREER_URL)
    assert (target.scheme == "https" and target.hostname in LIVE_ALLOWED_HOSTS
            and target.username is None and target.password is None
            and target.port in (None, 443))

    database_url = f"sqlite:///{(tmp_path / 'live-company-discovery.sqlite').as_posix()}"
    output_dir = tmp_path / "outputs"
    store = Store(database_url)
    try:
        store.migrate()
        store.bind_workspace_mode("PERSONAL")
        assert store.count("companies") == 0, "test store must start without a known employer"

        # Only this local API call is POST. The external career/ATS requests are
        # performed by the real production worker and public GET clients below.
        app = create_app(
            store,
            "http://127.0.0.1:18799",
            output_dir,
            redis_url="redis://127.0.0.1:1/0",
            operation_mode="PERSONAL",
        )
        with TestClient(app) as client:
            response = client.post("/companies/discovery", json={
                "company_name": LIVE_COMPANY_NAME,
                "company_website": LIVE_CAREER_URL,
                "reason": "PREVIOUSLY_UNSEEN_COMPANY",
            })
        assert response.status_code == 202, "local discovery admission did not queue"
        queued = response.json()
        assert queued.get("status") == "QUEUED"
        company_id = queued["company_id"]
        operation_id = queued["operation_id"]

        # execute_operation is the production worker entry point. PERSONAL mode
        # selects PublicCompanyClient; no HTTP transport/client is mocked here.
        monkeypatch.setenv("MJOBS_OPERATION_MODE", "PERSONAL")
        monkeypatch.setenv("MJOBS_DATABASE_URL", database_url)
        monkeypatch.setenv("MJOBS_OUTPUT_DIR", str(output_dir))
        worker_result = execute_operation(operation_id)
        assert worker_result.get("status") == "COMPLETED", (
            f"production discovery worker did not complete: {worker_result.get('status')}"
        )

        # A new Store and API instance prove the source and job rows survived close/reopen.
        store.engine.dispose()
        reopened = Store(database_url)
        try:
            read_app = create_app(
                reopened,
                "http://127.0.0.1:18799",
                output_dir,
                redis_url="redis://127.0.0.1:1/0",
                operation_mode="PERSONAL",
            )
            with TestClient(read_app) as client:
                sources_response = client.get(
                    "/company-sources", params={"company_id": company_id},
                )
                jobs_response = client.get(
                    "/jobs", params={"company": LIVE_COMPANY_NAME, "limit": 500},
                )
            assert sources_response.status_code == 200
            assert jobs_response.status_code == 200
            sources = sources_response.json()["company_sources"]
            jobs = jobs_response.json()["jobs"]
            source = next((row for row in sources
                           if row.get("provider") in {"greenhouse", "lever"}
                           and isinstance(row.get("tenant"), str) and row["tenant"]
                           and row.get("career_url")), None)
            assert source is not None, "no durable supported ATS provider/tenant was read back"
            assert urlsplit(source["career_url"]).hostname in LIVE_ALLOWED_HOSTS, (
                "enrolled career URL left the fixed first-party host allowlist"
            )
            current_jobs = [row for row in jobs if row.get("company_id") == company_id]
            assert current_jobs, "no current job from the live discovery was durably read back"

            with reopened.engine.connect() as connection:
                application_count = connection.execute(
                    select(func.count()).select_from(m.applications)
                ).scalar_one()
                attempt_count = connection.execute(
                    select(func.count()).select_from(m.attempts)
                ).scalar_one()
                submission_audits = connection.execute(select(m.audit.c.id).where(
                    func.upper(m.audit.c.action).like("%SUBMIT%")
                )).all()
            assert application_count == 0
            assert attempt_count == 0
            assert submission_audits == []
        finally:
            reopened.engine.dispose()
    finally:
        store.engine.dispose()
