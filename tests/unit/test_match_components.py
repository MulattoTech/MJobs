"""Unit tests for services/matching/components.py (MJ-REQ-113)."""
import pytest

from services.matching.components import (
    evaluate_match_components,
    MATCH_DIMENSIONS,
    DEFAULT_DIMENSION_WEIGHTS,
)


PROFILE = {
    "id": "cand-1",
    "name": "Jordan Engineer",
    "preferences": {
        "desired_titles": ["DevOps Engineer", "Cloud Infrastructure Engineer"],
        "remote": "REMOTE",
        "employment_types": ["FULL_TIME"],
        "salary_min": 120000,
        "industries": ["Cloud Infrastructure", "FinTech"],
        "negative_keywords": ["legacy cobol", "on-call 24/7"],
    },
    "facts": [
        {"kind": "title", "text": "Senior DevOps Engineer", "user_approved": True, "verified": True},
        {"kind": "skill", "text": "Kubernetes", "user_approved": True, "verified": True},
        {"kind": "skill", "text": "Terraform", "user_approved": True, "verified": True},
        {"kind": "skill", "text": "AWS", "user_approved": True, "verified": True},
        {"kind": "experience", "text": "5 years managing production Kubernetes on AWS", "user_approved": True, "verified": True},
        {"kind": "education", "text": "Bachelor of Science in Computer Science", "user_approved": True, "verified": True},
        {"kind": "certification", "text": "AWS Certified Solutions Architect", "user_approved": True, "verified": True},
    ],
}


def test_match_components_all_dimensions_evaluated():
    job = {
        "id": "job-devops-1",
        "title": "Cloud Infrastructure Engineer",
        "company_id": "comp-cloud",
        "remote_type": "REMOTE_US",
        "skills": ["Kubernetes", "Terraform", "AWS"],
        "attributes": {
            "required_skills": ["Kubernetes", "Terraform"],
            "preferred_skills": ["AWS"],
            "experience_level": "SENIOR",
            "education_requirement": "BACHELORS_REQUIRED",
            "certifications": [{"name": "AWS Certified Solutions Architect"}],
            "salary_min": 130000,
            "salary_max": 160000,
            "employment_type": "FULL_TIME",
            "industry": "Cloud Infrastructure",
        },
        "description": "We are seeking a senior cloud infrastructure engineer to lead cloud automation.",
    }
    company_prefs = {"comp-cloud": "PREFERRED"}

    result = evaluate_match_components(job, PROFILE, company_preferences=company_prefs)

    assert result["score"] is not None
    assert result["score"] >= 85.0  # Should be an excellent match
    assert len(result["dimensions"]) == 12

    dims_by_name = {d["dimension"]: d for d in result["dimensions"]}

    # Title similarity
    assert dims_by_name["title_similarity"]["status"] == "KNOWN"
    assert dims_by_name["title_similarity"]["raw_score"] == 100.0

    # Required skills
    assert dims_by_name["required_skills"]["status"] == "KNOWN"
    assert dims_by_name["required_skills"]["raw_score"] == 100.0

    # Preferred skills
    assert dims_by_name["preferred_skills"]["status"] == "KNOWN"
    assert dims_by_name["preferred_skills"]["raw_score"] == 100.0

    # Experience
    assert dims_by_name["experience"]["status"] == "KNOWN"

    # Education
    assert dims_by_name["education"]["status"] == "KNOWN"
    assert dims_by_name["education"]["raw_score"] == 100.0

    # Certifications
    assert dims_by_name["certifications"]["status"] == "KNOWN"
    assert dims_by_name["certifications"]["raw_score"] == 100.0

    # Remote location
    assert dims_by_name["remote_location"]["status"] == "KNOWN"
    assert dims_by_name["remote_location"]["raw_score"] == 100.0

    # Salary
    assert dims_by_name["salary"]["status"] == "KNOWN"
    assert dims_by_name["salary"]["raw_score"] == 100.0

    # Employment type
    assert dims_by_name["employment_type"]["status"] == "KNOWN"
    assert dims_by_name["employment_type"]["raw_score"] == 100.0

    # Industry
    assert dims_by_name["industry"]["status"] == "KNOWN"
    assert dims_by_name["industry"]["raw_score"] == 100.0

    # Company preference
    assert dims_by_name["company_preference"]["status"] == "KNOWN"
    assert dims_by_name["company_preference"]["raw_score"] == 100.0

    # Negative requirements (clean)
    assert dims_by_name["negative_requirements"]["status"] == "KNOWN"
    assert dims_by_name["negative_requirements"]["raw_score"] == 100.0
    assert result["negative_signals_triggered"] == []


def test_negative_requirements_trigger_severely_penalizes_score():
    job_with_disallowed = {
        "id": "job-cobol",
        "title": "Cloud Engineer with Legacy Cobol",
        "description": "Must be willing to perform on-call 24/7 and maintain legacy cobol systems.",
        "skills": ["Kubernetes"],
    }
    result = evaluate_match_components(job_with_disallowed, PROFILE)

    assert "legacy cobol" in [t.casefold() for t in result["negative_signals_triggered"]]
    assert "on-call 24/7" in [t.casefold() for t in result["negative_signals_triggered"]]
    assert result["score"] == 0.0


def test_unknown_dimensions_do_not_penalize_score():
    """Missing signals drop from active weight denominator so they do not artificially penalize."""
    sparse_job = {
        "id": "job-sparse",
        "title": "DevOps Engineer",
        "skills": ["Kubernetes"],
        # No salary, no education, no industry, no certs
    }
    result = evaluate_match_components(sparse_job, PROFILE)

    assert result["score"] is not None
    # Score should still reflect matched title and required skills (around 100%)
    assert result["score"] >= 80.0

    dims_by_name = {d["dimension"]: d for d in result["dimensions"]}
    assert dims_by_name["salary"]["status"] == "UNKNOWN"
    assert dims_by_name["education"]["status"] == "UNKNOWN"
    assert dims_by_name["certifications"]["status"] == "UNKNOWN"


def test_custom_weights_configuration():
    job = {
        "id": "job-weights",
        "title": "Cloud Architect",
        "skills": ["Kubernetes"],
        "attributes": {"salary_min": 150000, "salary_max": 180000},
    }
    # Configure custom weights: heavily weight salary
    custom_weights = {
        "salary": 80.0,
        "title_similarity": 20.0,
    }
    result = evaluate_match_components(job, PROFILE, weights=custom_weights)

    dims_by_name = {d["dimension"]: d for d in result["dimensions"]}
    assert dims_by_name["salary"]["weight"] == 80.0
    assert dims_by_name["title_similarity"]["weight"] == 20.0
    assert result["score"] is not None
