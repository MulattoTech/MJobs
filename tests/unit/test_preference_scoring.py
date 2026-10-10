"""Preference weighting must be grounded in candidate/job facts, never source scores."""
from copy import deepcopy

import pytest

from services.matching.profile_scoring import resolve_and_compose_profile_scoring

DIMENSIONS = ("title", "salary", "work_arrangement", "employment_type", "remote_location")
PROFILE = {
    "preferences": {
        "desired_roles": ["Systems Administrator"],
        "work_arrangements": ["REMOTE"],
        "employment_types": ["FULL_TIME"],
        "salary_expectations": "$90000-$110000 per year",
    },
    "contact": {"country": "US", "state": "OK"},
    "facts": [],
}
JOB = {
    "title": "Systems Administrator", "remote_type": "REMOTE_US",
    "attributes": {
        "employment_type": "FULL_TIME", "remote_regions": ["US"],
        "salary_min": 95000, "salary_max": 115000,
        "salary_currency": "USD", "salary_period": "YEAR",
        "salary_source": "EMPLOYER_PROVIDED",
    },
}


def compose(job=None, candidate=PROFILE, *, weights=None, thresholds=None, baseline=60):
    config = {
        "id": "synthetic-config", "version": "v1", "schema_version": 1,
        "role_family": "A configuration label, not a job title",
        "weights": weights or {"skills": 50, **{key: 10 for key in DIMENSIONS}},
        "thresholds": thresholds or {},
    }
    return resolve_and_compose_profile_scoring(
        JOB if job is None else job, {"score": baseline},
        role_family=config["role_family"], profiles_by_role_family={config["role_family"]: config},
        candidate_profile=candidate,
    )


def rows(result):
    return {row["dimension"]: row for row in result["scoring_contributions"]}


def test_five_evidence_backed_preferences_change_the_selected_score_without_mutation():
    before = deepcopy((PROFILE, JOB))
    result = compose()
    assert result["score"] == 80
    assert (PROFILE, JOB) == before
    assert "eligibility" not in result
    for dimension in DIMENSIONS:
        row = rows(result)[dimension]
        assert row["raw_score"] == 100
        assert row["weighted_contribution"] == 10
        assert row["provenance"] == {
            "source": "DETERMINISTIC_PROFILE_JOB_FIT",
            "evidence_ref": f"match:fit_factors/{dimension}",
        }
        assert "advisory" in row["explanation"]
    assert "90000" not in str(result) and "110000" not in str(result)
    assert "OK" not in str(result) and PROFILE["preferences"]["desired_roles"][0] not in str(result)


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_missing_profile_cannot_be_replaced_with_employer_supplied_fit_score(dimension):
    job = deepcopy(JOB)
    job["attributes"]["specialized_signals"] = {
        dimension: {"score": 100, "provenance": {"source": "EMPLOYER_PROVIDED"}},
    }
    result = compose(job, candidate=None, weights={"skills": 25, dimension: 75})
    assert result["score"] == 60
    assert rows(result)[dimension]["raw_score"] is None
    assert rows(result)[dimension]["weighted_contribution"] is None
    assert rows(result)[dimension]["provenance"] == {"source": "UNKNOWN"}


def test_known_nonmatches_are_zero_advisory_contributions_not_unknown_or_ineligibility():
    job = deepcopy(JOB)
    job.update(title="Dental Assistant", remote_type="REMOTE_COUNTRY")
    job["attributes"].update(employment_type="PART_TIME", remote_regions=["Canada"],
                            salary_min=50000, salary_max=60000)
    candidate = deepcopy(PROFILE)
    candidate["preferences"]["work_arrangements"] = ["ON_SITE"]
    result = compose(job, candidate)
    assert result["score"] == 30
    for dimension in DIMENSIONS:
        assert rows(result)[dimension]["raw_score"] == 0
        assert rows(result)[dimension]["weighted_contribution"] == 0
    assert "eligible" not in result and "missing" not in result


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_source_scores_cannot_override_a_real_preference_mismatch(dimension):
    job = deepcopy(JOB)
    job.update(title="Dental Assistant", remote_type="REMOTE_COUNTRY")
    job["attributes"].update(employment_type="PART_TIME", remote_regions=["Canada"],
                            salary_min=50000, salary_max=60000,
                            specialized_signals={dimension: {
                                "score": 100, "provenance": {"source": "FORGED_PROFILE_FIT"},
                            }})
    candidate = deepcopy(PROFILE)
    candidate["preferences"]["work_arrangements"] = ["ON_SITE"]
    result = compose(job, candidate, weights={"skills": 50, dimension: 50})
    assert result["score"] == 30
    assert rows(result)[dimension]["raw_score"] == 0
    assert "FORGED_PROFILE_FIT" not in str(result)


@pytest.mark.parametrize("change", [
    {"salary_currency": "CAD"}, {"salary_period": "HOUR"},
    {"salary_source": "AI_ESTIMATE"}, {"salary_min": float("nan")},
    {"salary_max": None}, {"salary_min": True},
])
def test_salary_uncertainty_is_excluded_from_the_denominator(change):
    job = deepcopy(JOB)
    job["attributes"].update(change)
    result = compose(job, weights={"skills": 20, "salary": 80})
    assert result["score"] == 60
    assert rows(result)["salary"]["raw_score"] is None


@pytest.mark.parametrize("candidate", [None, {}, {"preferences": {}}, {"preferences": "invalid"}])
def test_sparse_or_malformed_profile_preferences_are_neutral(candidate):
    result = compose(candidate=candidate)
    assert result["score"] == 60
    assert all(rows(result)[dimension]["raw_score"] is None for dimension in DIMENSIONS)


def test_remote_country_or_region_uncertainty_is_not_inferred_from_an_address():
    job = deepcopy(JOB)
    job["attributes"]["remote_regions"] = ["US", "Unspecified region"]
    result = compose(job, weights={"skills": 20, "remote_location": 80})
    assert result["score"] == 60
    assert rows(result)["remote_location"]["raw_score"] is None


def test_zero_weights_do_not_appear_and_all_unknown_falls_back_to_legacy():
    result = compose(weights={"skills": 100, **{key: 0 for key in DIMENSIONS}})
    assert set(rows(result)) == {"skills"} and result["score"] == 60
    result = compose(candidate=None, weights={"skills": 0, "title": 100})
    assert result["score"] == 60 and rows(result)["title"]["raw_score"] is None


def test_unconfigured_scoring_does_not_change_the_legacy_contract():
    result = resolve_and_compose_profile_scoring(
        JOB, {"score": 61.23}, role_family=None, candidate_profile=PROFILE,
    )
    assert result["score"] == 61.23
    assert set(rows(result)) == {"skills"}


@pytest.mark.parametrize("dimension,field,value", [
    ("employment_type", "employment_type", "UNRECOGNIZED_CONTRACT"),
    ("employment_type", "employment_type", True),
    ("employment_type", "employment_type", ["FULL_TIME"]),
    ("remote_location", "remote_regions", ["US", True]),
    ("remote_location", "remote_regions", "US"),
    ("salary", "salary_source", []),
])
def test_malformed_metadata_is_unknown_not_a_negative_or_positive_fact(dimension, field, value):
    job = deepcopy(JOB)
    job["attributes"][field] = value
    result = compose(job, weights={"skills": 20, dimension: 80})
    assert result["score"] == 60
    assert rows(result)[dimension]["raw_score"] is None


@pytest.mark.parametrize("dimension,key,value", [
    ("title", "desired_roles", [123]),
    ("work_arrangement", "work_arrangements", ["MAYBE_REMOTE"]),
    ("employment_type", "employment_types", ["UNKNOWN"]),
    ("employment_type", "employment_types", "FULL_TIME"),
])
def test_malformed_preferences_do_not_become_negative_qualification_claims(dimension, key, value):
    candidate = deepcopy(PROFILE)
    candidate["preferences"][key] = value
    result = compose(candidate=candidate, weights={"skills": 20, dimension: 80})
    assert result["score"] == 60
    assert rows(result)[dimension]["raw_score"] is None


@pytest.mark.parametrize("dimension", ["work_arrangement", "remote_location"])
def test_unsupported_remote_type_cannot_gain_points_from_its_remote_prefix(dimension):
    job = deepcopy(JOB)
    job["remote_type"] = "REMOTE_UNVERIFIED"
    result = compose(job, weights={"skills": 20, dimension: 80})
    assert result["score"] == 60
    assert rows(result)[dimension]["raw_score"] is None
