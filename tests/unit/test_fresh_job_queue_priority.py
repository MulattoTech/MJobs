"""Unit tests for services/matching/queue_priority.py (MJ-REQ-119)."""
from datetime import datetime, timedelta, timezone
import pytest

from services.matching.queue_priority import (
    calculate_job_queue_priority,
    order_queue_by_fresh_priority,
    summarize_queue_distribution,
    to_operation_dispatch_params,
)


NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_critical_tier_fresh_excellent_match():
    job = {
        "id": "job-1",
        "title": "Senior Systems Engineer",
        "posted_at": (NOW - timedelta(hours=6)).isoformat(),
    }
    result = calculate_job_queue_priority(job, {"score": 92.0}, now=NOW)

    assert result["tier"] == 0
    assert result["queue_priority"] == "CRITICAL"
    assert result["dispatch_priority"] == "HIGH"
    assert result["match_quality"] == "EXCELLENT"
    assert result["freshness_band"] == "FRESH"
    assert result["age_hours"] == 6.0
    assert result["expedited"] is True
    assert result["deferred"] is False
    assert "Expedited queue priority" in result["explanation"]


def test_high_tier_fresh_strong_match():
    job = {
        "id": "job-2",
        "title": "Cloud Administrator",
        "posted_at": (NOW - timedelta(hours=12)).isoformat(),
    }
    result = calculate_job_queue_priority(job, 78.0, now=NOW)

    assert result["tier"] == 1
    assert result["queue_priority"] == "HIGH"
    assert result["dispatch_priority"] == "HIGH"
    assert result["match_quality"] == "STRONG"
    assert result["freshness_band"] == "FRESH"
    assert result["expedited"] is True


def test_high_tier_recent_excellent_match():
    job = {
        "id": "job-3",
        "title": "Platform Architect",
        "attributes": {"reported_posted_at": (NOW - timedelta(hours=48)).isoformat()},
    }
    result = calculate_job_queue_priority(job, 88.5, now=NOW)

    assert result["tier"] == 1
    assert result["queue_priority"] == "HIGH"
    assert result["dispatch_priority"] == "HIGH"
    assert result["freshness_band"] == "RECENT"
    assert result["expedited"] is True


def test_normal_tier_recent_strong_match():
    job = {
        "id": "job-4",
        "title": "DevOps Engineer",
        "posted_at": (NOW - timedelta(hours=36)).isoformat(),
    }
    result = calculate_job_queue_priority(job, 80.0, now=NOW)

    assert result["tier"] == 2
    assert result["queue_priority"] == "NORMAL"
    assert result["dispatch_priority"] == "NORMAL"
    assert result["expedited"] is False
    assert result["deferred"] is False


def test_deferred_tier_aging_job():
    job = {
        "id": "job-5",
        "title": "Network Specialist",
        "posted_at": (NOW - timedelta(days=10)).isoformat(),
    }
    result = calculate_job_queue_priority(job, 78.0, now=NOW)

    assert result["tier"] == 3
    assert result["queue_priority"] == "DEFERRED"
    assert result["dispatch_priority"] == "LOW"
    assert result["freshness_band"] == "AGING"
    assert result["deferred"] is True


def test_stale_tier_old_job_regardless_of_high_score():
    job = {
        "id": "job-6",
        "title": "Security Analyst",
        "posted_at": (NOW - timedelta(days=45)).isoformat(),
    }
    # Even with a 95% match, an ancient 45-day-old posting is demoted to STALE tier
    result = calculate_job_queue_priority(job, 95.0, now=NOW)

    assert result["tier"] == 4
    assert result["queue_priority"] == "STALE"
    assert result["dispatch_priority"] == "LOW"
    assert result["freshness_band"] == "STALE"
    assert result["deferred"] is True
    assert "stale posting" in result["explanation"]


def test_stale_tier_poor_match_regardless_of_freshness():
    job = {
        "id": "job-7",
        "title": "Graphic Designer",
        "posted_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    # A fresh posting that is a poor match (<40%) is demoted
    result = calculate_job_queue_priority(job, 32.0, now=NOW)

    assert result["tier"] == 4
    assert result["queue_priority"] == "STALE"
    assert result["match_quality"] == "POOR"
    assert result["deferred"] is True


def test_background_tier_missing_evidence():
    job = {"id": "job-8", "title": "Database Admin"}
    result = calculate_job_queue_priority(job, None, now=NOW)

    assert result["tier"] == 5
    assert result["queue_priority"] == "BACKGROUND"
    assert result["match_quality"] == "UNKNOWN"
    assert result["freshness_band"] == "UNKNOWN"
    assert result["age_hours"] is None


def test_order_queue_new_excellent_matches_processed_before_stale():
    """Verify core requirement: new excellent matches are processed before stale lower-quality jobs."""
    jobs = [
        # Stale 40-day-old job with high match
        {"id": "stale-high", "title": "Stale Senior Lead", "posted_at": (NOW - timedelta(days=40)).isoformat()},
        # Fresh 2-hour-old job with excellent match
        {"id": "fresh-excellent", "title": "Fresh Architect", "posted_at": (NOW - timedelta(hours=2)).isoformat()},
        # Recent 2-day-old job with strong match
        {"id": "recent-strong", "title": "Recent Engineer", "posted_at": (NOW - timedelta(days=2)).isoformat()},
        # Fresh 4-hour-old job with weak match
        {"id": "fresh-weak", "title": "Fresh Unaligned", "posted_at": (NOW - timedelta(hours=4)).isoformat()},
    ]
    matches = {
        "stale-high": 95.0,
        "fresh-excellent": 91.0,
        "recent-strong": 82.0,
        "fresh-weak": 35.0,
    }

    ordered = order_queue_by_fresh_priority(jobs, matches, now=NOW)
    ordered_ids = [j["id"] for j in ordered]

    # Fresh excellent MUST come first!
    assert ordered_ids[0] == "fresh-excellent"
    # Recent strong comes next
    assert ordered_ids[1] == "recent-strong"
    # Stale jobs and weak jobs are at the back
    assert set(ordered_ids[2:]) == {"stale-high", "fresh-weak"}


def test_order_queue_deterministic_stability():
    jobs = [
        {"id": "job-b", "title": "Beta Analyst", "posted_at": (NOW - timedelta(hours=8)).isoformat()},
        {"id": "job-a", "title": "Alpha Analyst", "posted_at": (NOW - timedelta(hours=8)).isoformat()},
    ]
    matches = {"job-a": 88.0, "job-b": 88.0}

    # Same tier (Critical), same score (88.0), same age (8h) -> title tie-breaker determines order
    ordered1 = order_queue_by_fresh_priority(jobs, matches, now=NOW)
    ordered2 = order_queue_by_fresh_priority(list(reversed(jobs)), matches, now=NOW)

    assert [j["id"] for j in ordered1] == ["job-a", "job-b"]
    assert [j["id"] for j in ordered2] == ["job-a", "job-b"]


def test_to_operation_dispatch_params():
    job = {"id": "job-1", "posted_at": (NOW - timedelta(hours=4)).isoformat()}
    priority = calculate_job_queue_priority(job, 90.0, now=NOW)
    params = to_operation_dispatch_params(priority, {"profile_id": "prof-1", "job_id": "job-1"})

    assert params["dispatch_priority"] == "HIGH"
    assert params["queue_tier"] == 0
    assert params["queue_priority"] == "CRITICAL"
    assert params["fresh_job_priority"] is True
    assert params["profile_id"] == "prof-1"


def test_summarize_queue_distribution():
    ranked_jobs = [
        {"queue_priority": "CRITICAL"},
        {"queue_priority": "HIGH"},
        {"queue_priority": "NORMAL"},
        {"queue_priority": "DEFERRED"},
        {"queue_priority": "STALE"},
        {"queue_priority": "BACKGROUND"},
    ]
    summary = summarize_queue_distribution(ranked_jobs)

    assert summary["total_queued"] == 6
    assert summary["expedited_count"] == 2
    assert summary["normal_count"] == 1
    assert summary["deferred_count"] == 2
    assert summary["background_count"] == 1


def test_future_timestamp_handling():
    # Future timestamp (e.g. clock drift) should not crash or cause negative age
    job = {"id": "future-job", "posted_at": (NOW + timedelta(hours=2)).isoformat()}
    result = calculate_job_queue_priority(job, 88.0, now=NOW)

    assert result["age_hours"] == 0.0
    assert result["freshness_band"] == "FRESH"
    assert result["tier"] == 0
