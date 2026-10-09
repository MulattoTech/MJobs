"""Application priority is an explainable advisory separate from match fit."""

from datetime import datetime, timezone

import pytest

from services.discovery.application_priority import explain_application_priority


NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def test_known_signals_produce_deterministic_weighted_explanations():
    signals = {
        "match_score": 80,
        "posted_at": "2026-10-08T12:00:00Z",
        "salary_status": "ALIGNS",
        "company_preference": "NEUTRAL",
        "direct_employer_availability": "STAFFING_AGENCY",
        "application_complexity": "MEDIUM",
        "source_freshness_at": "2026-10-07T12:00:00Z",
        "expires_at": "2026-10-11T12:00:00Z",
    }

    result = explain_application_priority(signals, now=NOW)

    assert result["priority_score"] == 81.5
    assert result["priority_band"] == "HIGH"
    assert result["coverage_percent"] == 100
    contributions = {row["key"]: row["contribution"] for row in result["factors"]}
    assert contributions == {
        "match": 20,
        "posting_age": 15,
        "salary": 15,
        "company_preference": 6,
        "direct_employer_availability": 7.5,
        "application_complexity": 6,
        "source_freshness": 8,
        "expiration_risk": 4,
    }
    assert all(row["explanation"] and row["source"] for row in result["factors"])
    assert explain_application_priority(signals, now=NOW) == result


def test_missing_or_unusable_signals_stay_unknown_and_reduce_coverage():
    result = explain_application_priority({
        "match_score": 80,
        "posted_at": "not-a-date",
        "salary_status": "UNKNOWN",
        "company_preference": None,
        "direct_employer_availability": "UNVERIFIED",
        "application_complexity": "EXTREME",
        "source_freshness_at": None,
        "expires_at": None,
    }, now=NOW)

    dimensions = {row["key"]: row for row in result["factors"]}
    assert dimensions["match"]["status"] == "KNOWN"
    assert dimensions["match"]["contribution"] == 20
    for name in (
        "posting_age", "salary", "company_preference",
        "direct_employer_availability", "application_complexity",
        "source_freshness", "expiration_risk",
    ):
        assert dimensions[name]["status"] == "UNKNOWN"
        assert dimensions[name]["contribution"] is None
    assert result["coverage_percent"] == 25
    assert result["priority_score"] == 80
    assert set(result["unknowns"]) == set(dimensions) - {"match"}


def test_old_posting_and_expiration_signals_lower_priority_without_excluding_job():
    result = explain_application_priority({
        "posted_at": "2026-08-25T12:00:00Z",
        "source_freshness_at": "2026-08-25T12:00:00Z",
        "expires_at": "2026-10-08T12:00:00Z",
    }, now=NOW)

    dimensions = {row["key"]: row for row in result["factors"]}
    assert dimensions["posting_age"]["status"] == "KNOWN"
    assert dimensions["posting_age"]["contribution"] == 3
    assert dimensions["source_freshness"]["contribution"] == 2
    assert dimensions["expiration_risk"]["contribution"] == 0
    assert result["priority_band"] == "LOW"
    assert result["priority_score"] == pytest.approx(16.67)
    assert result["advisory"] is True
    assert result["affects_match_score"] is False
    assert not any(key in result for key in (
        "eligible", "eligibility", "reject", "rejected", "submit", "submission",
    ))


def test_agency_and_nonmatching_salary_remain_advisory_priority_factors():
    result = explain_application_priority({
        "salary_status": "NO_DIRECT_MATCH",
        "direct_employer_availability": "STAFFING_AGENCY",
    }, now=NOW)

    dimensions = {row["key"]: row for row in result["factors"]}
    assert dimensions["salary"]["status"] == "KNOWN"
    assert dimensions["salary"]["contribution"] == 3
    assert dimensions["direct_employer_availability"]["contribution"] == 7.5
    assert result["advisory"] is True
    assert "eligible" not in result
    assert "reject" not in result


@pytest.mark.parametrize("signals", [
    {},
    {"match_score": True},
    {"match_score": 101},
    {"match_score": float("nan")},
])
def test_no_valid_signals_yields_unknown_priority_not_a_default_score(signals):
    result = explain_application_priority(signals, now=NOW)

    assert result["priority_score"] is None
    assert result["priority_band"] == "UNKNOWN"
    assert result["coverage_percent"] == 0
    assert all(row["status"] == "UNKNOWN" for row in result["factors"])


def test_future_or_timezone_ambiguous_dates_do_not_create_recency_or_expiry_scores():
    result = explain_application_priority({
        "posted_at": "2026-10-10T12:00:00Z",
        "source_freshness_at": "2026-10-09T13:00:00+01:00",
        "expires_at": "2026-10-10T12:00:00",
    }, now=NOW)

    factors = {row["key"]: row for row in result["factors"]}
    assert factors["posting_age"]["status"] == "UNKNOWN"
    assert factors["source_freshness"]["status"] == "KNOWN"
    assert factors["expiration_risk"]["status"] == "UNKNOWN"
    assert set(result["unknowns"]) == {"match", "salary", "company_preference",
                                       "direct_employer_availability", "application_complexity",
                                       "posting_age", "expiration_risk"}
