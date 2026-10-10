"""Persisted job-list and explanation acceptance for bounded positive career evidence."""
from copy import deepcopy
from unittest.mock import patch

from fastapi.testclient import TestClient
from packages.persistence.store import Store
from tests.integration.test_preference_weighting import (
    app_for,configure,create_workspace,explanation_url,rescore,snapshot,
)
from tests.unit.test_positive_skill_evidence import fact


def save_evidence(store, negative=True):
    candidate=deepcopy(store.profile("fixture"))
    candidate["facts"]=[
        fact("Linux. No Active Directory experience." if negative else "Linux and Active Directory",id="skill-fact"),
        fact("Supported Linux. I have never administered Active Directory." if negative else
             "Administered Linux and Active Directory.",kind="experience",id="experience-fact"),
    ]
    store.save_profile(candidate)


def test_negative_skill_and_experience_mentions_do_not_inflate_persisted_scores_after_restart(tmp_path):
    store=create_workspace(tmp_path)
    try:
        save_evidence(store)
        expected_profile=deepcopy(store.profile("fixture"))
        with TestClient(app_for(store,tmp_path)) as client,patch(
                "socket.create_connection",side_effect=AssertionError("No provider calls")):
            saved=configure(client,{"skills":50,"experience":50})
            rescore(store)
            response=client.get(explanation_url())
            assert response.status_code==200,response.text
            body=response.json()
            assert body["score"]==50
            experience=next(row for row in body["scoring_contributions"] if row["dimension"]=="experience")
            assert experience["raw_score"]==50
            assert "1 of 2" in experience["explanation"]
            assert "withheld" in experience["explanation"]
            assert experience["provenance"]["source"]=="USER_APPROVED_EXPERIENCE_FACTS"
            jobs=client.get("/jobs",params={"profile_id":"fixture","search_profile_id":"preference-search"}).json()["jobs"]
            assert all(job["match_score"]==50 for job in jobs)
            row=next(row for row in jobs[0]["match_components"]["requirements"] if row["skill"]=="Active Directory")
            assert row["evidence_reason"]=="EXPLICIT_NEGATIVE_OR_UNCERTAIN_EVIDENCE"
            assert row["profile_evidence"]=="NO_APPROVED_EVIDENCE"
            assert store.profile("fixture")==expected_profile
            before=snapshot(store)
        store.engine.dispose()
        store=Store(f"sqlite:///{(tmp_path/'preferences.db').as_posix()}")
        with TestClient(app_for(store,tmp_path)) as client:
            assert client.get(explanation_url()).json()==body
            assert snapshot(store)==before
    finally:
        store.engine.dispose()


def test_explicit_career_fact_correction_can_restore_credit_only_after_recalculation(tmp_path):
    store=create_workspace(tmp_path)
    try:
        save_evidence(store)
        with TestClient(app_for(store,tmp_path)) as client:
            configure(client,{"skills":50,"experience":50})
            rescore(store)
            assert client.get(explanation_url()).json()["score"]==50
            before_version=store.profile("fixture")["version"]
            save_evidence(store,negative=False)
            assert store.profile("fixture")["version"]!=before_version
            corrected=deepcopy(store.profile("fixture"))
            rescore(store)
            body=client.get(explanation_url()).json()
            assert body["score"]==100
            assert all(row["raw_score"]==100 for row in body["scoring_contributions"])
            assert store.profile("fixture")==corrected
    finally:
        store.engine.dispose()
