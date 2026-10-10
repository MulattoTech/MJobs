"""Read-only, scoped and bounded draft-weight preview through the mounted API."""
from copy import deepcopy
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from packages.persistence import models as m
from tests.integration.test_preference_weighting import (
    CONFIG_URL, app_for, configure, create_workspace, criteria, rescore, snapshot,
)

PREVIEW_URL = CONFIG_URL + "/preview"
DRAFT = {"schema_version": 1, "role_family": "Draft only", "weights": {"skills": 0, "title": 100}, "thresholds": {}}


@pytest.fixture
def workspace(tmp_path):
    store = create_workspace(tmp_path)
    with TestClient(app_for(store, tmp_path)) as client:
        saved = configure(client)
        rescore(store)
        yield store, client, saved
    store.engine.dispose()


def request(saved, draft=None):
    return {"expected_version": saved["version"] if saved else None, "profile": deepcopy(draft or DRAFT)}


def test_preview_changes_rank_math_without_writing_any_table_or_calling_a_provider(workspace):
    store, client, saved = workspace
    before = snapshot(store)
    with patch("socket.create_connection", side_effect=AssertionError("No network during preview")):
        response = client.post(PREVIEW_URL, json=request(saved))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["read_only"] is True
    assert body["profile_id"] == "fixture" and body["search_profile_id"] == "preference-search"
    assert body["current_version"] == saved["version"]
    assert body["candidate_limit"] == 100
    assert body["scanned_count"] == 3 and body["eligible_count"] == 3
    assert body["truncated"] is False
    rows = {row["job_id"]: row for row in body["jobs"]}
    assert rows["aligned-job"]["current_score"] == 75
    assert rows["aligned-job"]["draft_score"] == 100
    assert rows["aligned-job"]["delta"] == 25
    assert rows["unknown-job"]["draft_score"] == 50
    assert rows["unknown-job"]["draft_contributions"][0]["raw_score"] is None
    assert rows["mismatch-job"]["draft_score"] == 0
    assert [row["draft_rank"] for row in body["jobs"]] == [1, 2, 3]
    assert len(body["snapshot_id"]) == 64
    assert snapshot(store) == before
    again = client.post(PREVIEW_URL, json=request(saved)).json()
    assert again["snapshot_id"] == body["snapshot_id"]
    assert again["jobs"] == body["jobs"]
    assert "preferences@example.test" not in str(body)
    assert "$90000" not in str(body) and "facts" not in body


def test_preview_enforces_saved_search_filters_and_never_changes_them(workspace):
    store, client, saved = workspace
    scoped = criteria(store)
    scoped["include_titles"] = ["Systems Administrator"]
    with store.engine.begin() as connection:
        connection.execute(update(m.search_profiles).where(m.search_profiles.c.id == "preference-search").values(criteria=scoped))
    before = snapshot(store)
    body = client.post(PREVIEW_URL, json=request(saved)).json()
    assert [row["job_id"] for row in body["jobs"]] == ["aligned-job"]
    assert body["eligible_count"] == 1
    assert snapshot(store) == before


def test_cross_profile_or_stale_match_rows_are_not_preview_candidates(workspace):
    store, client, saved = workspace
    with store.engine.begin() as connection:
        connection.execute(m.matches.delete().where(m.matches.c.job_id == "aligned-job"))
        connection.execute(m.matches.insert().values(
            id="other-profile-only", profile_id="other", profile_version=store.profile("other")["version"],
            job_id="aligned-job", score=99, missing=[], components={},
        ))
        connection.execute(update(m.matches).where(m.matches.c.job_id == "mismatch-job").values(profile_version="stale"))
    body = client.post(PREVIEW_URL, json=request(saved)).json()
    assert [row["job_id"] for row in body["jobs"]] == ["unknown-job"]
    wrong = client.post(PREVIEW_URL.replace("profiles/fixture/", "profiles/other/"), json=request(saved))
    assert wrong.status_code == 404


@pytest.mark.parametrize("bad", [
    {"expected_version": "old-version", "profile": DRAFT},
    {"expected_version": None, "profile": DRAFT},
])
def test_stale_editor_version_is_rejected_without_a_write(workspace, bad):
    store, client, _ = workspace
    before = snapshot(store)
    response = client.post(PREVIEW_URL, json=bad)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SCORING_PREVIEW_VERSION_CONFLICT"
    assert snapshot(store) == before


@pytest.mark.parametrize("weights", [{"title": True}, {"title": 101}, {"title": -1}, {},
                                       {f"signal_{i}": 1 for i in range(33)}])
def test_invalid_or_unbounded_drafts_are_rejected(workspace, weights):
    store, client, saved = workspace
    draft = {**DRAFT, "weights": weights}
    before = snapshot(store)
    response = client.post(PREVIEW_URL, json=request(saved, draft))
    assert response.status_code == 422
    assert snapshot(store) == before


def test_preview_with_no_current_matches_is_explicitly_empty_not_global(workspace):
    store, client, saved = workspace
    with store.engine.begin() as connection:
        connection.execute(m.matches.delete())
    response = client.post(PREVIEW_URL, json=request(saved))
    assert response.status_code == 200
    assert response.json()["jobs"] == []
    assert response.json()["scanned_count"] == 0


def test_advanced_workspace_filters_fail_closed_instead_of_silently_being_ignored(workspace):
    store, client, saved = workspace
    scoped = criteria(store)
    scoped["job_filters"] = {"application_pipeline_status": "APPLIED"}
    with store.engine.begin() as connection:
        connection.execute(update(m.search_profiles).where(m.search_profiles.c.id == "preference-search").values(criteria=scoped))
    before = snapshot(store)
    response = client.post(PREVIEW_URL, json=request(saved))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SCORING_PREVIEW_FILTERS_UNSUPPORTED"
    assert snapshot(store) == before


def test_bounded_preview_reports_truncation_and_never_labels_sample_ranks_global(workspace):
    store, client, saved = workspace
    version = store.profile("fixture")["version"]
    with store.engine.begin() as connection:
        template = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == "aligned-job")).mappings().one())
        for number in range(105):
            job_id = f"bounded-{number:03}"
            connection.execute(m.jobs.insert().values({**template, "id": job_id, "requisition": job_id}))
            connection.execute(m.matches.insert().values(
                id=f"match-{number}",job_id=job_id,profile_id="fixture",profile_version=version,
                score=50,missing=[],components={},
            ))
    before = snapshot(store)
    body = client.post(PREVIEW_URL, json=request(saved)).json()
    assert body["truncated"] is True
    assert body["scanned_count"] == 100
    assert len(body["jobs"]) == 100
    assert body["jobs"][-1]["draft_rank"] == 100
    assert "not all jobs" in body["limitation"]
    assert snapshot(store) == before


def test_a_saved_source_filter_is_not_satisfied_by_an_unrelated_source(workspace):
    store, client, saved = workspace
    with store.engine.begin() as connection:
        connection.execute(update(m.search_profiles).where(m.search_profiles.c.id == "preference-search")
                           .values(source_ids=["selected-source"]))
        connection.execute(m.postings.insert().values(
            id="source-posting",job_id="aligned-job",source="selected-source",source_job_id="source-1",
            source_url="https://synthetic.example.test/jobs/1",application_url="https://synthetic.example.test/apply/1",
            raw={"synthetic":True},fetched_at="2026-10-10T00:00:00Z",
        ))
    response = client.post(PREVIEW_URL, json=request(saved))
    assert response.status_code == 200, response.text
    assert [row["job_id"] for row in response.json()["jobs"]] == ["aligned-job"]
