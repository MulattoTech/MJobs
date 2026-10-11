"""Unit tests for services/matching/application_priority.py (MJ-REQ-118 & MJ-REQ-119)."""
from datetime import datetime, timezone
import pytest

from services.matching.application_priority import (
    explain_priority_divergence,
    evaluate_job_priority_full,
)


NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_divergence_strongly_aligned():
    div = explain_priority_divergence(85.0, 84.0)
    assert div["classification"] == "STRONGLY_ALIGNED"
    assert div["delta"] == -1.0
    assert "reinforce each other" in div["summary"]


def test_divergence_match_dominant():
    factors = [
        {"key": "match", "label": "Match", "score": 92.0, "status": "KNOWN"},
        {"key": "application_complexity", "label": "Application complexity", "score": 20.0, "status": "KNOWN"},
        {"key": "direct_employer_availability", "label": "Direct-employer availability", "score": 60.0, "status": "KNOWN"},
    ]
    div = explain_priority_divergence(92.0, 68.0, factors)

    assert div["classification"] == "MATCH_DOMINANT"
    assert div["delta"] == -24.0
    assert len(div["top_drags"]) >= 1
    assert div["top_drags"][0]["key"] == "application_complexity"
    assert "offset by operational friction" in div["summary"]


def test_divergence_priority_boosted():
    factors = [
        {"key": "match", "label": "Match", "score": 70.0, "status": "KNOWN"},
        {"key": "posting_age", "label": "Posting age", "score": 100.0, "status": "KNOWN"},
        {"key": "company_preference", "label": "Company preference", "score": 100.0, "status": "KNOWN"},
        {"key": "direct_employer_availability", "label": "Direct-employer availability", "score": 100.0, "status": "KNOWN"},
    ]
    div = explain_priority_divergence(70.0, 88.0, factors)

    assert div["classification"] == "PRIORITY_BOOSTED"
    assert div["delta"] == 18.0
    assert len(div["top_boosters"]) >= 2
    assert "elevated by favorable handling conditions" in div["summary"]


def test_divergence_unknown():
    div1 = explain_priority_divergence(None, 80.0)
    assert div1["classification"] == "UNKNOWN"
    assert div1["delta"] is None

    div2 = explain_priority_divergence(80.0, None)
    assert div2["classification"] == "UNKNOWN"
    assert div2["delta"] is None


def test_evaluate_job_priority_full():
    job = {
        "id": "job-test-101",
        "title": "Staff Reliability Engineer",
        "posted_at": "2026-10-10T08:00:00Z",
    }
    signals = {
        "posted_at": "2026-10-10T08:00:00Z",
        "salary_status": "ALIGNS",
        "direct_employer_availability": "DIRECT_EMPLOYER",
        "application_complexity": "LOW",
    }
    match = {"score": 88.0}

    result = evaluate_job_priority_full(job, match, now=NOW, signals=signals)

    assert result["job_id"] == "job-test-101"
    assert result["match_score"] == 88.0
    assert result["priority_score"] is not None
    assert result["priority_band"] == "HIGH"
    assert result["advisory"] is True
    assert result["affects_match_score"] is False

    # Check divergence analysis
    assert result["divergence"]["classification"] in ("STRONGLY_ALIGNED", "MODERATELY_ALIGNED", "PRIORITY_BOOSTED")

    # Check queue tier
    assert result["queue"]["tier"] == 0
    assert result["queue"]["queue_priority"] == "CRITICAL"
    assert result["queue"]["expedited"] is True
