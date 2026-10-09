"""Selected saved-search scoring uses only its own persisted profile config."""

from copy import deepcopy

import pytest

from services.matching.selected_search_scoring import compose_selected_search_match


def _profile(profile_id, role_family, weights, thresholds=None):
    return {
        "id": profile_id,
        "version": f"version-{profile_id}",
        "schema_version": 1,
        "role_family": role_family,
        "weights": weights,
        "thresholds": thresholds or {},
    }


def _job(signal_score=90):
    signal = {
        "score": signal_score,
        "provenance": {"source": "EMPLOYER_DISCLOSED", "evidence_ref": "posting:ai-signal"},
    }
    return {"attributes": {"specialized_signals": {"ai_intensity": signal}}}


def _legacy(score=40):
    return {"score": score, "components": {"preserved": True}, "missing": []}


def test_selected_search_user_weights_change_score_and_keep_signal_attribution_isolated():
    first_criteria = {
        "role_scoring_profile": _profile(
            "search-one-profile", "Systems", {"skills": 25, "ai_intensity": 75},
        ),
        "job_filters": {"salary_min": "65000", "remote_type": "REMOTE"},
    }
    second_criteria = {
        "role_scoring_profile": _profile(
            "search-two-profile", "Systems", {"skills": 80, "ai_intensity": 20},
        ),
        "job_filters": {"salary_min": "90000", "remote_type": "ONSITE"},
    }
    first_before, second_before = deepcopy(first_criteria), deepcopy(second_criteria)

    first = compose_selected_search_match(_job(), _legacy(), first_criteria)
    second = compose_selected_search_match(_job(), _legacy(), second_criteria)

    assert first["score"] == 77.5
    assert first["scoring_profile"] == {
        "requested_role_family": "systems", "selected_role_family": "Systems",
        "profile_id": "search-one-profile", "profile_version": "version-search-one-profile",
        "selection": "ROLE_FAMILY",
    }
    first_rows = {row["dimension"]: row for row in first["scoring_contributions"]}
    assert first_rows["skills"]["weighted_contribution"] == 10.0
    assert first_rows["skills"]["provenance"] == {"source": "LEGACY_MATCH_BASELINE"}
    assert first_rows["ai_intensity"]["weighted_contribution"] == 67.5
    assert first_rows["ai_intensity"]["provenance"] == {
        "source": "EMPLOYER_DISCLOSED", "evidence_ref": "posting:ai-signal",
    }

    assert second["score"] == 50.0
    assert second["scoring_profile"]["profile_id"] == "search-two-profile"
    assert first["scoring_profile"]["profile_id"] != second["scoring_profile"]["profile_id"]
    assert first_criteria == first_before
    assert second_criteria == second_before


def test_missing_saved_search_profile_preserves_legacy_default_and_unknown_rows():
    baseline = _legacy(64.5)

    result = compose_selected_search_match(_job(), baseline, {"job_filters": {"remote_type": "REMOTE"}})

    assert result["score"] == 64.5
    assert result["scoring_profile"]["selection"] == "LEGACY_DEFAULT"
    assert result["scoring_profile"]["profile_id"] is None
    assert result["scoring_contributions"][0]["weighted_contribution"] == 64.5

    configured = compose_selected_search_match(
        {"attributes": {}}, baseline,
        {"role_scoring_profile": _profile("unknown-signal", "Cloud", {
            "skills": 0, "ai_intensity": 100,
        })},
    )
    assert configured["score"] == 64.5
    rows = {row["dimension"]: row for row in configured["scoring_contributions"]}
    assert rows["ai_intensity"]["raw_score"] is None
    assert rows["ai_intensity"]["weighted_contribution"] is None
    assert rows["ai_intensity"]["provenance"] == {"source": "UNKNOWN"}


@pytest.mark.parametrize("criteria", [
    None,
    [],
    {"role_scoring_profile": "not-an-object"},
    {"role_scoring_profile": {
        "id": "profile", "version": "v1", "schema_version": 1,
        "role_family": "Systems", "weights": {"skills": float("nan")}, "thresholds": {},
    }},
])
def test_malformed_search_or_scoring_profile_is_rejected(criteria):
    with pytest.raises(ValueError):
        compose_selected_search_match(_job(), _legacy(), criteria)
