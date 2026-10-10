"""Synthetic regression for supported negative, aspirational and conflicting skill facts."""
from copy import deepcopy

import pytest

from services.discovery.matching import build_explanation
from services.matching.profile_scoring import resolve_and_compose_profile_scoring

JOB={"title":"Synthetic platform role","skills":["Linux","Kubernetes"],
     "attributes":{"skill_evidence":"structured_required_skills"}}


def fact(text, kind="skill", **changes):
    return {"id":"synthetic-fact","kind":kind,"text":text,"verified":True,
            "user_approved":True,"confidence":100,"source":"synthetic review",**changes}


def profile(*facts):
    return {"facts":list(facts),"preferences":{},"contact":{}}


@pytest.mark.parametrize("text,expected", [
    ("I know Linux but have no Kubernetes experience.", ["Linux"]),
    ("Managed Linux. No hands-on Kubernetes experience.", ["Linux"]),
    ("Linux; I have never used K8s.", ["Linux"]),
    ("Linux. I do not know Kubernetes.", ["Linux"]),
    ("Linux; Kubernetes experience: none.", ["Linux"]),
    ("Linux. Kubernetes knowledge is unknown.", ["Linux"]),
    ("Linux. I haven't worked with Kubernetes.", ["Linux"]),
    ("Linux. Without practical Kubernetes experience.", ["Linux"]),
    ("Linux. Lacking Kubernetes knowledge.", ["Linux"]),
    ("Linux. Interested in learning Kubernetes.", ["Linux"]),
    ("Linux. I plan to use Kubernetes.", ["Linux"]),
    ("Linux. I would like to learn K8s.", ["Linux"]),
    ("No experience with Linux or Kubernetes.", []),
    ("I have never used Linux, Kubernetes.", []),
    ("Linux and Kubernetes experience: none.", []),
    ("Linux; I have not yet used Kubernetes.", ["Linux"]),
    ("Linux; I haven?t worked with K8s.", ["Linux"]),
    ("Linux and Kubernetes", ["Kubernetes","Linux"]),
    ("Not only Linux but also Kubernetes.", ["Kubernetes","Linux"]),
    ("Managed Linux without downtime.", ["Linux"]),
    ("Kubernetes administration with no downtime.", ["Kubernetes"]),
])
def test_only_positive_mentions_support_skills_and_experience(text, expected):
    candidate=profile(fact(text))
    before=deepcopy(candidate)
    explained=build_explanation(JOB,candidate)
    assert explained["components"]["matched"]==expected
    assert explained["score"]==50*len(expected)
    assert candidate==before
    candidate=profile(fact(text,kind="experience"))
    config={"id":"synthetic","version":"v1","schema_version":1,"role_family":"Synthetic",
            "weights":{"skills":0,"experience":100},"thresholds":{}}
    composed=resolve_and_compose_profile_scoring(JOB,{"score":40},role_family="Synthetic",
        profiles_by_role_family={"Synthetic":config},candidate_profile=candidate)
    row=composed["scoring_contributions"][0]
    assert row["raw_score"]==(50*len(expected) if expected else None)
    assert composed["score"]==(50*len(expected) if expected else 40)


def test_approved_conflicting_facts_need_review_instead_of_selective_positive_scoring():
    candidate=profile(fact("Kubernetes"),fact("I have no Kubernetes experience.",id="negative"),fact("Linux",id="positive"))
    explained=build_explanation(JOB,candidate)
    assert explained["score"]==50
    row=next(row for row in explained["components"]["requirements"] if row["skill"]=="Kubernetes")
    assert row["profile_evidence"]=="NO_APPROVED_EVIDENCE"
    assert row["evidence_reason"]=="CONFLICTING_APPROVED_EVIDENCE"
    assert "review" in row["explanation"].lower()
    assert "lacks" not in row["explanation"].lower()


def test_unapproved_negative_text_cannot_invalidate_approved_positive_evidence():
    candidate=profile(fact("Kubernetes"),fact("No Kubernetes experience",user_approved=False,id="unapproved"))
    assert build_explanation(JOB,candidate)["score"]==50


@pytest.mark.parametrize("confidence", [float("nan"),float("inf"),True,101,-1,None,"100"])
def test_invalid_confidence_is_not_positive_evidence(confidence):
    assert build_explanation(JOB,profile(fact("Linux and Kubernetes",confidence=confidence)))["score"]==0


def test_supported_negative_evidence_is_explained_without_echoing_private_fact_text():
    private="No Kubernetes experience. Private workplace detail not for output."
    explained=build_explanation(JOB,profile(fact(private)))
    row=next(row for row in explained["components"]["requirements"] if row["skill"]=="Kubernetes")
    assert row["evidence_reason"]=="EXPLICIT_NEGATIVE_OR_UNCERTAIN_EVIDENCE"
    assert "Private workplace detail" not in str(explained)
    assert "does not prove" in explained["components"]["limitation"]


def test_custom_exact_skill_and_non_skill_fact_types_keep_existing_boundaries():
    job={**JOB,"skills":["SyntheticCustomTool"]}
    assert build_explanation(job,profile(fact("SyntheticCustomTool")))["score"]==100
    assert build_explanation(JOB,profile(fact("Linux and Kubernetes",kind="untrusted_note")))["score"]==0
