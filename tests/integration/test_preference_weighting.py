"""Real SQLite/API acceptance for preference weights and durable explanations."""
from copy import deepcopy
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import recalculate
from services.discovery.search_profiles import save_search_profile
from services.discovery.search_semantics import evaluate_search
from tests.unit.test_preference_scoring import DIMENSIONS, JOB, PROFILE

WEIGHTS = {"skills": 50, **{key: 10 for key in DIMENSIONS}}
CONFIG_URL = "/profiles/fixture/search-profiles/preference-search/role-scoring-profile"


def explanation_url(job_id="aligned-job", profile_id="fixture", search_id="preference-search"):
    return f"/profiles/{profile_id}/search-profiles/{search_id}/jobs/{job_id}/match-explanation"


def create_workspace(root):
    store = Store(f"sqlite:///{(root / 'preferences.db').as_posix()}")
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    profile = {
        **deepcopy(PROFILE), "id": "fixture", "name": "Synthetic Preference Profile",
        "email": "preferences@example.test", "answers": [],
        "facts": [{"id": "linux", "kind": "skill", "text": "Linux",
                   "verified": True, "user_approved": True, "confidence": 100,
                   "source": "synthetic reviewed fixture"}],
    }
    for profile_id in ("fixture", "other"):
        store.save_profile({**deepcopy(profile), "id": profile_id})
        save_search_profile(store, {
            "id": "preference-search" if profile_id == "fixture" else "other-search",
            "profile_id": profile_id, "name": f"{profile_id} preferences", "enabled": False,
            "include_titles": [], "exclude_titles": [], "required_keywords": [],
            "optional_keywords": [], "excluded_keywords": [], "exact_phrases": [],
            "technology_aliases": {}, "remote": "ANY", "employment_types": [], "source_ids": [],
        })
    aligned = deepcopy(JOB)
    mismatch = deepcopy(JOB)
    mismatch.update(title="Dental Assistant", remote_type="REMOTE_COUNTRY")
    mismatch["attributes"].update(employment_type="PART_TIME", remote_regions=["Canada"],
                                 salary_min=50000, salary_max=60000)
    unknown = {"title": "", "remote_type": "UNKNOWN", "attributes": {}}
    with store.engine.begin() as connection:
        connection.execute(m.companies.insert().values(
            id="synthetic-company", name="Synthetic Preference Employer", domain="preference.example.test",
        ))
        for job_id, job in (("aligned-job", aligned), ("mismatch-job", mismatch), ("unknown-job", unknown)):
            job["attributes"]["skill_evidence"] = "structured_required_skills"
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id="synthetic-company", requisition=job_id,
                description="Linux and Active Directory synthetic fixture.",
                skills=["Linux", "Active Directory"], location="Synthetic location", **job,
            ))
            connection.execute(m.routes.insert().values(
                id=f"route-{job_id}", job_id=job_id, company_id="synthetic-company", provider="GREENHOUSE",
                tenant="synthetic-only", requisition=job_id,
                url=f"https://preference.example.test/apply/{job_id}",
            ))
    return store


def app_for(store, root):
    return create_app(store, "http://127.0.0.1:1", root,
                      "redis://127.0.0.1:1/0", operation_mode="FIXTURE")


def configure(client, weights=None, expected_version=None):
    response = client.put(CONFIG_URL, json={
        "expected_version": expected_version,
        "profile": {"schema_version": 1, "role_family": "Preference weighting",
                    "weights": WEIGHTS if weights is None else weights, "thresholds": {}},
    })
    assert response.status_code == 200, response.text
    return response.json()["profile"]


def criteria(store, search_id="preference-search"):
    with store.engine.connect() as connection:
        return deepcopy(connection.execute(select(m.search_profiles.c.criteria).where(
            m.search_profiles.c.id == search_id,
        )).scalar_one())


def rescore(store):
    recalculate(store, "fixture", candidate_job_ids=["aligned-job", "mismatch-job", "unknown-job"],
                scoring_criteria=criteria(store))


def snapshot(store):
    with store.engine.connect() as connection:
        return {table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables}


def test_weights_rank_real_jobs_persist_reopen_and_preserve_scope_and_hard_filters(tmp_path):
    store = create_workspace(tmp_path)
    try:
        before_profile = store.profile("fixture")
        before_criteria = criteria(store)
        before_other = criteria(store, "other-search")
        with store.engine.connect() as connection:
            jobs = [dict(row) for row in connection.execute(select(m.jobs)).mappings()]
        eligibility = {job["id"]: evaluate_search(before_criteria, job)["matched"] for job in jobs}
        with TestClient(app_for(store, tmp_path)) as client, patch(
                "socket.create_connection", side_effect=AssertionError("External network is forbidden")):
            saved = configure(client)
            rescore(store)
            results = {}
            for job_id, expected in (("aligned-job", 75), ("mismatch-job", 35), ("unknown-job", 50)):
                response = client.get(explanation_url(job_id))
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["score"] == expected
                assert body["scoring_profile"]["version"] == saved["version"]
                contributions = body["scoring_contributions"]
                assert sum(row["weighted_contribution"] or 0 for row in contributions) == expected
                results[job_id] = body
            response = client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "preference-search"})
            assert response.status_code == 200, response.text
            assert [job["id"] for job in response.json()["jobs"]] == ["aligned-job", "unknown-job", "mismatch-job"]
            wrong = client.get(explanation_url(profile_id="other"))
            assert wrong.status_code == 404
            before_reads = snapshot(store)
        assert store.profile("fixture") == before_profile
        after_criteria = criteria(store)
        assert {key: value for key, value in after_criteria.items() if key != "role_scoring_profile"} == before_criteria
        assert criteria(store, "other-search") == before_other
        assert {job["id"]: evaluate_search(after_criteria, job)["matched"] for job in jobs} == eligibility
        store.engine.dispose()
        store = Store(f"sqlite:///{(tmp_path / 'preferences.db').as_posix()}")
        with TestClient(app_for(store, tmp_path)) as client:
            for job_id, body in results.items():
                assert client.get(explanation_url(job_id)).json() == body
            assert client.get(CONFIG_URL).json()["profile"] == saved
            assert snapshot(store) == before_reads
    finally:
        store.engine.dispose()


def test_stale_weight_edits_require_recalculation_and_cannot_return_false_current_scores(tmp_path):
    store = create_workspace(tmp_path)
    try:
        with TestClient(app_for(store, tmp_path)) as client:
            saved = configure(client)
            rescore(store)
            changed = configure(client, {"skills": 50, "title": 50}, saved["version"])
            stale = client.get(explanation_url())
            assert stale.status_code == 409
            assert stale.json()["detail"]["code"] == "MATCH_EXPLANATION_STALE_CONTEXT"
            before_stale_save = snapshot(store)
            rejected = client.put(CONFIG_URL, json={"expected_version": saved["version"], "profile": {
                "schema_version": 1, "role_family": "Stale editor", "weights": {"skills": 100}, "thresholds": {},
            }})
            assert rejected.status_code == 409
            assert snapshot(store) == before_stale_save
            rescore(store)
            body = client.get(explanation_url()).json()
            assert body["score"] == 75
            assert body["scoring_profile"]["version"] == changed["version"]
            assert {row["dimension"] for row in body["scoring_contributions"]} == {"skills", "title"}
    finally:
        store.engine.dispose()
