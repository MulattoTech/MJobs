"""Reject only malformed Remotive postings and retain valid durable job evidence."""
import copy
from datetime import datetime, timedelta, timezone
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.errors import SourceError
from tests.integration.test_spine import PROFILE


def posting(native_id, title="Synthetic Python Engineer"):
    return {
        "id": native_id,
        "title": title,
        "company_name": "Synthetic Remotive Employer",
        "url": "https://remotive.com/remote-jobs/software-dev/synthetic-1001",
        "description": "<p>Build Python services.</p>",
        "publication_date": "2026-10-01T12:00:00",
        "candidate_required_location": "Worldwide",
        "salary": "",
    }


@pytest.mark.parametrize("invalid_id", ["\u00b2", "\u2460", "9" * 641],
                         ids=["superscript", "circled", "oversized-numeric-string"])
def test_invalid_posting_does_not_abort_valid_batch_and_repeated_collection(tmp_path, invalid_id):
    database_url = f"sqlite:///{(tmp_path / 'remotive.db').as_posix()}"
    store = Store(database_url)
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(PROFILE)
        source_id = configure_source(store, "default", provider="remotive")
        valid = posting("001001")
        invalid = posting(invalid_id, title="Rejected Synthetic Posting")
        payload = {"jobs": [invalid, valid]}
        original = copy.deepcopy(payload)
        requests = []

        def transport(request):
            requests.append(str(request.url))
            assert request.method == "GET"
            assert str(request.url) == "https://remotive.com/api/remote-jobs"
            return httpx.Response(200, json=payload)

        for pass_number in range(2):
            if pass_number:
                with pytest.raises(SourceError, match="^SOURCE_COOLDOWN$"):
                    collect_source(store, source_id, "fixture",
                                   transport=httpx.MockTransport(transport))
                assert len(requests) == 1
                # Model an elapsed poll interval only in this disposable database.
                prior_attempt = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
                with store.engine.begin() as connection:
                    connection.execute(update(m.job_sources).where(
                        m.job_sources.c.id == source_id).values(last_attempt=prior_attempt))
            result = collect_source(store, source_id, "fixture",
                                    transport=httpx.MockTransport(transport))
            assert result["jobs_seen"] == 1
            assert result["rejected"] == 1
            assert result["direct_application"] is False
            store.engine.dispose()
            store = Store(database_url)

        with store.engine.connect() as connection:
            jobs = connection.execute(select(m.jobs)).mappings().all()
            postings = connection.execute(select(m.postings)).mappings().all()
            snapshots = connection.execute(select(m.posting_snapshots)).mappings().all()
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
        assert len(jobs) == len(postings) == len(snapshots) == 1
        assert jobs[0]["requisition"] == "1001"
        assert postings[0]["raw"]["id"] == "001001"
        assert snapshots[0]["raw"]["id"] == "001001"
        assert source["health_status"] == "PARTIAL"
        assert source["consecutive_failures"] == 0
        assert source["last_error"] is None
        assert store.count("applications") == 0
        assert payload == original
        assert len(requests) == 2

        with TestClient(create_app(store, "http://127.0.0.1:1", tmp_path,
                                  "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
            response = client.get(f"/jobs/{jobs[0]['id']}")
            assert response.status_code == 200
            detail = response.json()
            assert detail["title"] == valid["title"]
            assert detail["attributes"]["native_posting_id"] == 1001
            assert detail["attributes"]["submission_capability"] == "MANUAL_ONLY"
    finally:
        store.engine.dispose()
        sys.set_int_max_str_digits(previous_limit)
