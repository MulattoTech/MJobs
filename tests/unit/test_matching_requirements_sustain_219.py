"""Regression coverage for the matching requirement distinctions in MJ-REQ-218."""

import pytest

from services.matching.components import evaluate_match_components


def _dimension(result, name):
    return next(item for item in result["dimensions"] if item["dimension"] == name)


def _approved_skill(text):
    return {
        "kind": "skill",
        "text": text,
        "verified": True,
        "user_approved": True,
    }


def test_missing_required_skill_costs_more_than_missing_preferred_skill():
    job = {
        "title": "Systems Engineer",
        "attributes": {
            "required_skills": ["Python"],
            "preferred_skills": ["AWS"],
        },
    }
    matched_role = {"preferences": {"desired_roles": ["Systems Engineer"]}}

    required_missing = evaluate_match_components(
        job, {**matched_role, "facts": [_approved_skill("AWS")]}
    )
    preferred_missing = evaluate_match_components(
        job, {**matched_role, "facts": [_approved_skill("Python")]}
    )

    assert _dimension(required_missing, "required_skills")["raw_score"] == 0.0
    assert _dimension(required_missing, "preferred_skills")["raw_score"] == 100.0
    assert _dimension(preferred_missing, "required_skills")["raw_score"] == 100.0
    assert _dimension(preferred_missing, "preferred_skills")["raw_score"] == 0.0
    assert preferred_missing["score"] > required_missing["score"]


def test_unverified_candidate_skill_does_not_satisfy_a_required_skill():
    result = evaluate_match_components(
        {"attributes": {"required_skills": ["Python"]}},
        {
            "facts": [{
                "kind": "skill",
                "text": "Python",
                "verified": False,
                "user_approved": True,
            }],
        },
    )

    required = _dimension(result, "required_skills")
    assert required["raw_score"] == 0.0
    assert required["provenance"]["matched"] == []


def test_remote_preference_mismatch_is_a_known_low_fit():
    matching = evaluate_match_components(
        {"remote_type": "REMOTE_US"},
        {"preferences": {"work_arrangements": ["REMOTE"]}},
    )
    result = evaluate_match_components(
        {"remote_type": "ONSITE"},
        {"preferences": {"work_arrangements": ["REMOTE"]}},
    )

    assert _dimension(matching, "remote_location")["raw_score"] == 100.0
    remote = _dimension(result, "remote_location")
    assert remote["status"] == "KNOWN"
    assert remote["raw_score"] == 20.0
    assert remote["provenance"]["source"] == "LOCATION_ALIGNMENT"
    assert "mismatch" in remote["explanation"].casefold()


def test_salary_below_candidate_minimum_is_a_known_low_fit():
    matching = evaluate_match_components(
        {"attributes": {"salary_min": 130_000, "salary_max": 160_000}},
        {"preferences": {"salary_min": 120_000}},
    )
    result = evaluate_match_components(
        {"attributes": {"salary_min": 80_000, "salary_max": 100_000}},
        {"preferences": {"salary_min": 120_000}},
    )

    assert _dimension(matching, "salary")["raw_score"] == 100.0
    salary = _dimension(result, "salary")
    assert salary["status"] == "KNOWN"
    assert salary["raw_score"] == 20.0
    assert salary["provenance"]["source"] == "SALARY_BELOW_TARGET"
    assert "below target" in salary["explanation"].casefold()


def test_seniority_title_mismatch_scores_below_the_same_seniority():
    profile = {"preferences": {"desired_roles": ["Junior Systems Engineer"]}}
    matching_seniority = evaluate_match_components(
        {"title": "Junior Systems Engineer"}, profile
    )
    mismatched_seniority = evaluate_match_components(
        {"title": "Senior Systems Engineer"}, profile
    )

    assert _dimension(matching_seniority, "title_similarity")["raw_score"] == 100.0
    assert (
        _dimension(mismatched_seniority, "title_similarity")["raw_score"]
        < _dimension(matching_seniority, "title_similarity")["raw_score"]
    )
    assert mismatched_seniority["score"] < matching_seniority["score"]


def test_missing_preferred_education_is_less_penalized_than_required_education():
    profile = {"facts": []}
    required = evaluate_match_components(
        {"attributes": {"education_requirement": "BACHELORS_REQUIRED"}}, profile
    )
    preferred = evaluate_match_components(
        {"attributes": {"education_requirement": "BACHELORS_PREFERRED"}}, profile
    )

    required_education = _dimension(required, "education")
    preferred_education = _dimension(preferred, "education")
    assert required_education["raw_score"] == 40.0
    assert preferred_education["raw_score"] == 60.0
    assert preferred_education["provenance"]["source"] == "EDUCATION_PREFERRED"


def test_approved_degree_meets_preferred_and_required_education():
    profile = {
        "facts": [{
            "kind": "education",
            "text": "Bachelor of Science in Engineering",
            "user_approved": True,
        }],
    }
    required = evaluate_match_components(
        {"attributes": {"education_requirement": "BACHELORS_REQUIRED"}}, profile
    )
    preferred = evaluate_match_components(
        {"attributes": {"education_requirement": "BACHELORS_PREFERRED"}}, profile
    )

    assert _dimension(required, "education")["raw_score"] == 100.0
    assert _dimension(preferred, "education")["raw_score"] == 100.0


@pytest.mark.parametrize(
    "description",
    ["Incomplete bachelor's degree", "Bachelor's degree, not completed"],
)
def test_incomplete_preferred_degree_is_not_treated_as_a_required_failure(description):
    profile = {
        "facts": [{
            "kind": "education",
            "text": description,
            "user_approved": True,
        }],
    }
    preferred = evaluate_match_components(
        {"attributes": {"education_requirement": "BACHELORS_PREFERRED"}}, profile
    )
    required = evaluate_match_components(
        {"attributes": {"education_requirement": "BACHELORS_REQUIRED"}}, profile
    )

    education = _dimension(preferred, "education")
    assert education["raw_score"] == 60.0
    assert education["provenance"]["source"] == "EDUCATION_PREFERRED"
    assert _dimension(required, "education")["raw_score"] == 20.0
