"""Pure composition keeps profile selection and score contributions explainable."""

import math

import pytest

from services.matching.profile_scoring import (
    compose_profile_scoring,
    resolve_and_compose_profile_scoring,
)
from services.matching.scoring_profiles import resolve_role_scoring_profile


def _profile(profile_id, role_family, weights, thresholds=None):
    return {
        "id": profile_id,
        "version": f"version-{profile_id}",
        "schema_version": 1,
        "role_family": role_family,
        "weights": weights,
        "thresholds": thresholds or {},
    }


def _legacy(score=80.0):
    return {
        "score": score,
        "missing": ["Not included in composition"],
        "components": {"method": "legacy_skill_coverage", "sentinel": "preserve"},
    }


def _job(*signals):
    return {
        "title": "Systems Administrator",
        "description": "Role description can mention AI, but prose is not a signal.",
        "attributes": {"specialized_signals": dict(signals)},
    }


def _signal(score, source="EMPLOYER_DISCLOSED", evidence_ref="posting:signal"):
    provenance = {"source": source}
    if evidence_ref is not None:
        provenance["evidence_ref"] = evidence_ref
    return {"score": score, "provenance": provenance}


def _resolution(profile=None, *, family="systems", default_profile=None):
    catalog = {profile["role_family"]: profile} if profile else {}
    return resolve_role_scoring_profile(
        family, catalog, default_profile=default_profile,
    )


def test_legacy_default_preserves_baseline_and_names_fallback_with_contribution():
    legacy = _legacy(73.5)
    resolution = _resolution()

    result = compose_profile_scoring(_job(), legacy, resolution)

    assert result == {
        "score": 73.5,
        "scoring_profile": {
            "requested_role_family": "systems",
            "selected_role_family": None,
            "profile_id": None,
            "profile_version": None,
            "selection": "LEGACY_DEFAULT",
        },
        "scoring_contributions": [{
            "dimension": "skills", "weight": 100.0, "raw_score": 73.5,
            "weighted_contribution": 73.5,
            "explanation": "The legacy deterministic match score is preserved because no scoring profile was selected.",
            "provenance": {"source": "LEGACY_MATCH_BASELINE"},
        }],
    }
    assert legacy["components"]["sentinel"] == "preserve"


def test_selected_profile_composes_baseline_and_specialized_signal_by_weights():
    profile = _profile("systems-v2", "systems", {"skills": 25, "ai_intensity": 75})
    resolution = _resolution(profile)
    job = _job(("ai_intensity", _signal(60)))

    result = compose_profile_scoring(job, _legacy(80), resolution)

    assert result["score"] == 65.0
    assert result["scoring_profile"] == {
        "requested_role_family": "systems",
        "selected_role_family": "systems",
        "profile_id": "systems-v2",
        "profile_version": "version-systems-v2",
        "selection": "ROLE_FAMILY",
    }
    assert result["scoring_contributions"] == [
        {
            "dimension": "ai_intensity", "weight": 75.0, "raw_score": 60.0,
            "weighted_contribution": 45.0,
            "explanation": "The explicit normalized AI-intensity signal contributes to this advisory score.",
            "provenance": {"source": "EMPLOYER_DISCLOSED", "evidence_ref": "posting:signal"},
        },
        {
            "dimension": "skills", "weight": 25.0, "raw_score": 80.0,
            "weighted_contribution": 20.0,
            "explanation": "The legacy deterministic match score contributes as the skills baseline.",
            "provenance": {"source": "LEGACY_MATCH_BASELINE"},
        },
    ]


def test_data_driven_custom_signal_and_unrecognized_dimension_are_composed_safely():
    profile = _profile(
        "custom", "new role", {
            "skills": 20, "emerging_signal": 30, "ai_intensity": 50,
            "Future Signal": 10,
        },
    )
    resolution = _resolution(profile, family="NEW ROLE")
    job = _job(
        ("ai_intensity", _signal(80)),
        ("emerging_signal", _signal(60, "SOURCE_PROVIDED", "structured:emerging")),
    )

    result = compose_profile_scoring(job, _legacy(80), resolution)
    contributions = {row["dimension"]: row for row in result["scoring_contributions"]}

    assert result["score"] == 74.0
    assert contributions["emerging_signal"]["raw_score"] == 60.0
    assert contributions["emerging_signal"]["weighted_contribution"] == 18.0
    assert contributions["emerging_signal"]["provenance"] == {
        "source": "SOURCE_PROVIDED", "evidence_ref": "structured:emerging",
    }
    assert contributions["Future Signal"]["raw_score"] is None
    assert contributions["Future Signal"]["weighted_contribution"] is None
    assert contributions["ai_intensity"]["provenance"]["source"] == "EMPLOYER_DISCLOSED"


def test_default_profile_resolution_is_named_and_scored():
    default = _profile("general", "General", {"skills": 50, "ai_intensity": 50})
    resolution = resolve_role_scoring_profile(
        "uncatalogued family", {}, default_profile=default,
    )
    job = _job(("ai_intensity", _signal(90)))

    result = compose_profile_scoring(job, _legacy(70), resolution)

    assert result["score"] == 80.0
    assert result["scoring_profile"] == {
        "requested_role_family": "uncatalogued family",
        "selected_role_family": "General",
        "profile_id": "general",
        "profile_version": "version-general",
        "selection": "DEFAULT_PROFILE",
    }


def test_unknown_optional_signal_does_not_dilute_legacy_baseline():
    profile = _profile("ai-optional", "systems", {"skills": 30, "ai_intensity": 70})
    result = compose_profile_scoring(_job(), _legacy(81), _resolution(profile))

    assert result["score"] == 81.0
    rows = {row["dimension"]: row for row in result["scoring_contributions"]}
    assert rows["skills"]["weighted_contribution"] == 81.0
    assert rows["ai_intensity"]["raw_score"] is None
    assert rows["ai_intensity"]["weighted_contribution"] is None
    assert rows["ai_intensity"]["provenance"] == {"source": "UNKNOWN"}


def test_unknown_signals_only_fall_back_to_legacy_score_instead_of_zero():
    profile = _profile("only-ai", "systems", {"skills": 0, "ai_intensity": 100})
    result = compose_profile_scoring(_job(), _legacy(69), _resolution(profile))

    assert result["score"] == 69
    assert result["scoring_profile"]["selection"] == "ROLE_FAMILY"
    ai_signal = result["scoring_contributions"][0]
    assert ai_signal["raw_score"] is None
    assert ai_signal["weighted_contribution"] is None


def test_configured_threshold_is_a_soft_score_gate_not_an_eligibility_result():
    profile = _profile("thresholded", "systems", {"skills": 0, "ai_intensity": 100},
                       {"ai_intensity": 90})
    job = _job(("ai_intensity", _signal(80)))

    result = compose_profile_scoring(job, _legacy(72), _resolution(profile))

    assert result["score"] == 0.0
    assert "eligib" not in result
    contribution = result["scoring_contributions"][0]
    assert contribution["raw_score"] == 80.0
    assert contribution["weighted_contribution"] == 0.0
    assert "below the configured threshold" in contribution["explanation"]


@pytest.mark.parametrize("score", [None, -1, 101, True, math.nan, math.inf])
def test_invalid_legacy_baseline_is_rejected(score):
    with pytest.raises(ValueError):
        compose_profile_scoring(_job(), _legacy(score), _resolution())


def test_resolve_and_compose_applies_user_profile_weights_and_soft_threshold_with_attribution():
    profile = _profile(
        "cloud-v4", "Cloud Operations", {"skills": 40, "ai_intensity": 60},
        {"ai_intensity": 70},
    )
    job = _job(("ai_intensity", _signal(60, "USER_CONFIRMED_SOURCE", "job:explicit-ai-signal")))

    result = resolve_and_compose_profile_scoring(
        job, _legacy(80), role_family="  CLOUD   OPERATIONS ",
        profiles_by_role_family={"Cloud Operations": profile},
    )

    assert result["score"] == 32.0
    assert result["scoring_profile"] == {
        "requested_role_family": "cloud operations",
        "selected_role_family": "Cloud Operations",
        "profile_id": "cloud-v4",
        "profile_version": "version-cloud-v4",
        "selection": "ROLE_FAMILY",
    }
    rows = {row["dimension"]: row for row in result["scoring_contributions"]}
    assert rows["skills"]["weighted_contribution"] == 32.0
    assert rows["skills"]["provenance"] == {"source": "LEGACY_MATCH_BASELINE"}
    assert rows["ai_intensity"]["raw_score"] == 60.0
    assert rows["ai_intensity"]["weighted_contribution"] == 0.0
    assert rows["ai_intensity"]["provenance"] == {
        "source": "USER_CONFIRMED_SOURCE", "evidence_ref": "job:explicit-ai-signal",
    }
    assert "below the configured threshold" in rows["ai_intensity"]["explanation"]
    assert "eligibility" not in result


def test_resolve_and_compose_keeps_missing_configured_signal_explicitly_unknown():
    profile = _profile("cloud-v1", "cloud", {"skills": 50, "hourly_comp_suitability": 50})

    result = resolve_and_compose_profile_scoring(
        _job(), _legacy(76), role_family="cloud",
        profiles_by_role_family={"cloud": profile},
    )

    assert result["score"] == 76.0
    assert result["scoring_profile"]["profile_id"] == "cloud-v1"
    rows = {row["dimension"]: row for row in result["scoring_contributions"]}
    assert rows["hourly_comp_suitability"]["raw_score"] is None
    assert rows["hourly_comp_suitability"]["weighted_contribution"] is None
    assert rows["hourly_comp_suitability"]["provenance"] == {"source": "UNKNOWN"}


def test_experience_weight_uses_only_approved_profile_facts_and_normalized_job_skills():
    profile = _profile("systems-experience", "systems", {"skills": 40, "experience": 60})
    job = {
        **_job(("experience", _signal(100, "UNTRUSTED_JOB_SIGNAL"))),
        "skills": ["Active Directory", "Windows Server", "Azure"],
    }
    candidate_profile = {"facts": [
        {"id": "fact-approved", "kind": "experience",
         "text": "Managed Active Directory and Windows Server", "source": "resume.pdf",
         "confidence": 90, "verified": True, "user_approved": True},
        {"id": "fact-pending", "kind": "experience", "text": "Azure administration",
         "source": "resume.pdf", "confidence": 99, "verified": False,
         "user_approved": False},
    ]}

    result = compose_profile_scoring(
        job, _legacy(50), _resolution(profile), candidate_profile=candidate_profile,
    )
    contributions = {row["dimension"]: row for row in result["scoring_contributions"]}

    assert result["score"] == 60.0
    assert contributions["experience"]["raw_score"] == 66.67
    assert contributions["experience"]["weighted_contribution"] == 40.0
    assert contributions["experience"]["provenance"] == {
        "source": "USER_APPROVED_EXPERIENCE_FACTS",
    }
    assert "Active Directory" in contributions["experience"]["explanation"]
    assert "Azure" not in contributions["experience"]["explanation"]


def test_experience_weight_stays_unknown_without_approved_matching_facts():
    profile = _profile("systems-experience", "systems", {"skills": 0, "experience": 100})
    job = {
        **_job(("experience", _signal(100, "UNTRUSTED_JOB_SIGNAL"))),
        "skills": ["Active Directory"],
    }

    result = compose_profile_scoring(
        job, _legacy(73), _resolution(profile), candidate_profile={"facts": [{
            "id": "fact-pending", "kind": "experience", "text": "Active Directory",
            "source": "resume.pdf", "confidence": 99, "verified": True,
            "user_approved": False,
        }]},
    )
    experience = next(row for row in result["scoring_contributions"]
                      if row["dimension"] == "experience")

    assert result["score"] == 73
    assert experience["raw_score"] is None
    assert experience["weighted_contribution"] is None
    assert experience["provenance"] == {"source": "UNKNOWN"}


def test_resolve_and_compose_uses_explicit_default_and_legacy_fallbacks():
    default = _profile("general", "General", {"skills": 100})

    default_result = resolve_and_compose_profile_scoring(
        _job(), _legacy(67), role_family="new occupation",
        profiles_by_role_family={}, default_profile=default,
    )
    legacy_result = resolve_and_compose_profile_scoring(
        _job(), _legacy(67), role_family="another new occupation",
    )

    assert default_result["score"] == 67
    assert default_result["scoring_profile"]["selection"] == "DEFAULT_PROFILE"
    assert default_result["scoring_profile"]["profile_id"] == "general"
    assert legacy_result["score"] == 67
    assert legacy_result["scoring_profile"]["selection"] == "LEGACY_DEFAULT"
    assert legacy_result["scoring_contributions"][0]["weighted_contribution"] == 67
