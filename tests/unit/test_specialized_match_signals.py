"""Specialized role-match signals stay optional, evidence-backed, and advisory."""

import math

import pytest

from services.discovery.matching import build_explanation
from services.matching.specialized_signals import score_specialized_signals


def _job(signal=None, *, hourly_comp_suitability=None):
    specialized_signals = {}
    if signal is not None:
        specialized_signals["ai_intensity"] = signal
    if hourly_comp_suitability is not None:
        specialized_signals["hourly_comp_suitability"] = hourly_comp_suitability
    return {
        "id": "job-ai-support",
        "title": "AI Support Engineer",
        "description": "The role discusses substantial AI use in free text.",
        "attributes": {"specialized_signals": specialized_signals},
    }


def _known(score=80, source="EMPLOYER_DISCLOSED", evidence_ref="posting:ai-intensity"):
    provenance = {"source": source}
    if evidence_ref is not None:
        provenance["evidence_ref"] = evidence_ref
    return {"score": score, "provenance": provenance}


def test_known_signal_uses_explicit_provenance_and_normalized_weighted_contribution():
    scored = score_specialized_signals(
        _job(
            _known(), hourly_comp_suitability=_known(
                score=60, source="SOURCE_PROVIDED", evidence_ref="structured-hourly-fit",
            ),
        ), weights={"ai_intensity": 25, "hourly_comp_suitability": 75},
        thresholds={"ai_intensity": 50, "hourly_comp_suitability": 0},
    )

    assert scored["score"] == 65.0
    assert scored["contributions"] == [
        {
            "dimension": "ai_intensity", "weight": 25.0, "raw_score": 80.0,
            "weighted_contribution": 20.0,
            "explanation": "The explicit normalized AI-intensity signal contributes to this advisory score.",
            "provenance": {"source": "EMPLOYER_DISCLOSED", "evidence_ref": "posting:ai-intensity"},
        },
        {
            "dimension": "hourly_comp_suitability", "weight": 75.0, "raw_score": 60.0,
            "weighted_contribution": 45.0,
            "explanation": "The explicit normalized hourly_comp_suitability signal contributes to this advisory score.",
            "provenance": {"source": "SOURCE_PROVIDED", "evidence_ref": "structured-hourly-fit"},
        },
    ]


def test_threshold_is_advisory_and_contribution_math_uses_known_signal_weights_only():
    scored = score_specialized_signals(
        _job(_known(score=40, evidence_ref=None)),
        weights={"ai_intensity": 25, "hourly_comp_suitability": 75},
        thresholds={"ai_intensity": 50},
    )

    assert scored["score"] == 0.0
    contribution = scored["contributions"][0]
    assert contribution["raw_score"] == 40.0
    assert contribution["weighted_contribution"] == 0.0
    assert contribution["provenance"] == {"source": "EMPLOYER_DISCLOSED"}
    assert "below the configured threshold" in contribution["explanation"]


def test_unknown_signal_does_not_dilute_known_signal_score_or_contribution():
    scored = score_specialized_signals(
        _job(_known(score=80)),
        weights={"ai_intensity": 25, "hourly_comp_suitability": 75},
    )

    assert scored["score"] == 80.0
    ai_signal, hourly_signal = scored["contributions"]
    assert ai_signal["weighted_contribution"] == 80.0
    assert hourly_signal["raw_score"] is None
    assert hourly_signal["weighted_contribution"] is None
    assert hourly_signal["provenance"] == {"source": "UNKNOWN"}


@pytest.mark.parametrize("signal", [
    None,
    {"score": 80, "provenance": {}},
    {"score": 80, "provenance": {"source": "  "}},
    {"score": 80, "provenance": {"source": "x" * 129}},
    {"score": 80, "provenance": {"source": "EMPLOYER_DISCLOSED", "evidence_ref": " "}},
    {"score": 80, "provenance": {"source": "EMPLOYER_DISCLOSED", "evidence_ref": "x" * 2001}},
    {"score": math.nan, "provenance": {"source": "EMPLOYER_DISCLOSED"}},
    {"score": True, "provenance": {"source": "EMPLOYER_DISCLOSED"}},
    {"score": 101, "provenance": {"source": "EMPLOYER_DISCLOSED"}},
])
def test_missing_or_malformed_signal_stays_unknown_and_neutral(signal):
    scored = score_specialized_signals(
        _job(signal), weights={"ai_intensity": 100}, thresholds={"ai_intensity": 90},
    )

    assert scored["score"] is None
    assert scored["contributions"] == [{
        "dimension": "ai_intensity", "weight": 100.0, "raw_score": None,
        "weighted_contribution": None,
        "explanation": "ai_intensity is UNKNOWN because trustworthy normalized evidence is unavailable.",
        "provenance": {"source": "UNKNOWN"},
    }]


def test_free_text_ai_claim_is_not_a_specialized_signal_and_legacy_match_stays_unchanged():
    job = _job()
    scored = score_specialized_signals(job, weights={"ai_intensity": 100})
    legacy = build_explanation(job, {"preferences": {}, "facts": []})

    assert scored["score"] is None
    assert legacy["score"] == 0
    assert legacy["components"]["method"] == "description_skill_mentions"


@pytest.mark.parametrize("weights,thresholds", [
    ({"ai_intensity": -1}, {}),
    ({"ai_intensity": 101}, {}),
    ({"ai_intensity": True}, {}),
    ({"ai_intensity": math.inf}, {}),
    ({"bad dimension": 20}, {}),
    ({"ai_intensity": 100}, {"other_signal": 2}),
])
def test_invalid_signal_weights_thresholds_or_dimensions_are_rejected(weights, thresholds):
    with pytest.raises(ValueError):
        score_specialized_signals(_job(_known()), weights=weights, thresholds=thresholds)


def test_scoring_specialized_signal_does_not_mutate_job_metadata():
    job = _job(_known())
    original = {"attributes": {"specialized_signals": {"ai_intensity": _known()}}}

    score_specialized_signals(job, weights={"ai_intensity": 100})

    assert job["attributes"] == original["attributes"]
