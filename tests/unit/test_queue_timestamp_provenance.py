"""Queue recency preserves date evidence without inventing publication age."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from services.matching.queue_priority import (
    calculate_job_queue_priority, order_queue_by_fresh_priority,
)


NOW = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(hours=2)).isoformat()
OLD = (NOW - timedelta(days=180)).isoformat()


def job_with_dates(fields):
    job = {"id": "fixture", "title": "Engineer", "match_score": 90.0,
           "created_at": RECENT, "attributes": {"first_seen_at": RECENT}}
    fields = deepcopy(fields)
    job["attributes"].update(fields.pop("attributes", {}))
    job.update(fields)
    return job


@pytest.mark.parametrize(("fields", "source"), [
    ({"posted_at": OLD}, "job.posted_at"),
    ({"posted_at": RECENT, "attributes": {"reported_posted_at": OLD}},
     "attributes.reported_posted_at"),
])
def test_explicit_publication_precedes_recent_collection(fields, source):
    job = job_with_dates(fields)
    result = calculate_job_queue_priority(job, now=NOW)

    assert result["freshness_band"] == "STALE"
    assert result["age_hours"] == 180 * 24
    assert result["timestamp_source"] == source
    assert result["timestamp_kind"] == "POSTED"
    assert result["timestamp_state"] == "KNOWN"
    assert "stale posting" in result["explanation"].lower()
    assert result["expedited"] is False


@pytest.mark.parametrize(("fields", "source", "kind", "label"), [
    ({"attributes": {"first_seen_at": RECENT}},
     "attributes.first_seen_at", "FIRST_SEEN", "first-seen observation"),
    ({"created_at": RECENT}, "job.created_at", "CREATED", "local record"),
])
def test_observed_recency_does_not_claim_a_publication_date(fields, source, kind, label):
    job = {"id": "fixture", **fields}
    result = calculate_job_queue_priority(job, 90.0, now=NOW)

    assert result["age_hours"] == 2.0
    assert result["freshness_band"] == "FRESH"
    assert result["timestamp_source"] == source
    assert result["timestamp_kind"] == kind
    assert result["timestamp_state"] == "KNOWN"
    explanation = result["explanation"].lower()
    assert label in explanation
    assert "publication date unknown" in explanation
    assert "fresh posting" not in explanation
    assert "internal processing preference" in explanation
    assert "process immediately" not in explanation


@pytest.mark.parametrize("value", [
    "2026-11-09T01:00:00Z",
    "2026-11-09T01:00:00z",
    "2026-11-08T19:00:00-06:00",
    "2026-11-09T06:30:00+05:30",
    datetime(2026, 11, 9, 1, tzinfo=timezone.utc),
])
def test_timezone_equivalent_publication_instants_have_the_same_age(value):
    result = calculate_job_queue_priority({"id": "fixture", "posted_at": value}, 90, now=NOW)

    assert result["age_hours"] == 8.0
    assert result["freshness_band"] == "FRESH"
    assert result["timestamp_source"] == "job.posted_at"
    assert result["timestamp_kind"] == "POSTED"
    assert result["timestamp_state"] == "KNOWN"
    assert result["queue_priority"] == "CRITICAL"


@pytest.mark.parametrize(("fields", "source"), [
    ({"attributes": {"reported_posted_at": "not-a-date"}}, "attributes.reported_posted_at"),
    ({"attributes": {"reported_posted_at": "2026-11-09T07:00:00"}}, "attributes.reported_posted_at"),
    ({"attributes": {"reported_posted_at": datetime(2026, 11, 9, 7)}}, "attributes.reported_posted_at"),
    ({"attributes": {"reported_posted_at": "   "}}, "attributes.reported_posted_at"),
    ({"attributes": {"reported_posted_at": None, "reported_posted_at_raw": "invalid source date"}},
     "attributes.reported_posted_at_raw"),
    ({"attributes": {"reported_posted_at": "", "reported_posted_at_raw": "invalid source date"}},
     "attributes.reported_posted_at_raw"),
    ({"posted_at": RECENT, "attributes": {"reported_posted_at": "invalid source date"}},
     "attributes.reported_posted_at"),
    ({"posted_at": "not-a-date"}, "job.posted_at"),
    ({"posted_at": "2026-11-09T07:00:00"}, "job.posted_at"),
    ({"posted_at": "9999-12-31T23:59:59-12:00"}, "job.posted_at"),
])
def test_invalid_publication_evidence_is_not_replaced_by_recent_collection(fields, source):
    result = calculate_job_queue_priority(job_with_dates(fields), now=NOW)

    assert result["age_hours"] is None
    assert result["freshness_band"] == "UNKNOWN"
    assert result["timestamp_source"] == source
    assert result["timestamp_kind"] == "POSTED"
    assert result["timestamp_state"] == "INVALID"
    assert result["expedited"] is False
    assert "invalid" in result["explanation"].lower()
    assert "publication date unknown" in result["explanation"].lower()


@pytest.mark.parametrize("reported", [None, ""])
@pytest.mark.parametrize("raw", [None, ""])
def test_absent_publication_evidence_allows_an_explicit_observation_fallback(reported, raw):
    result = calculate_job_queue_priority(job_with_dates({"attributes": {
        "reported_posted_at": reported, "reported_posted_at_raw": raw,
    }}), now=NOW)

    assert result["timestamp_source"] == "attributes.first_seen_at"
    assert result["timestamp_kind"] == "FIRST_SEEN"
    assert result["timestamp_state"] == "KNOWN"
    assert result["age_hours"] == 2.0


def test_normalized_valid_publication_retains_its_original_raw_wording():
    job = job_with_dates({"attributes": {
        "reported_posted_at": OLD, "reported_posted_at_raw": "six months ago",
    }})
    before = deepcopy(job)
    result = calculate_job_queue_priority(job, now=NOW)

    assert result["timestamp_source"] == "attributes.reported_posted_at"
    assert result["timestamp_state"] == "KNOWN"
    assert result["age_hours"] == 180 * 24
    assert job == before


@pytest.mark.parametrize(("source", "kind"), [
    ("attributes.reported_posted_at", "POSTED"),
    ("job.posted_at", "POSTED"),
    ("attributes.first_seen_at", "FIRST_SEEN"),
    ("job.created_at", "CREATED"),
])
@pytest.mark.parametrize("offset", [timedelta(microseconds=1), timedelta(days=365)])
def test_future_evidence_keeps_its_source_and_unknown_age(source, kind, offset):
    value = (NOW + offset).isoformat()
    if source.startswith("attributes."):
        fields = {"attributes": {source.split(".")[1]: value}}
    else:
        fields = {source.split(".")[1]: value}
    job = {"id": "future-fixture", **fields}
    before = deepcopy(job)
    result = calculate_job_queue_priority(job, 90, now=NOW)

    assert result["age_hours"] is None
    assert result["freshness_band"] == "UNKNOWN"
    assert result["timestamp_source"] == source
    assert result["timestamp_kind"] == kind
    assert result["timestamp_state"] == "FUTURE"
    assert result["expedited"] is False
    assert "future" in result["explanation"].lower()
    assert job == before


def test_unqualified_update_timestamps_do_not_invent_publication_or_observation():
    result = calculate_job_queue_priority({
        "id": "unknown-fixture", "updated_at": RECENT,
        "attributes": {"updated_at": RECENT, "last_collected_at": RECENT},
    }, 90, now=NOW)

    assert result["age_hours"] is None
    assert result["freshness_band"] == "UNKNOWN"
    assert result["timestamp_source"] == "UNKNOWN"
    assert result["timestamp_kind"] == "UNKNOWN"
    assert result["timestamp_state"] == "UNKNOWN"
    assert result["expedited"] is False
    assert "publication date unknown" in result["explanation"].lower()


def test_ranked_output_carries_date_evidence_without_mutating_inputs():
    jobs = [
        {"id": "stale", "title": "Engineer", "match_score": 90, "posted_at": OLD,
         "attributes": {"first_seen_at": RECENT}},
        {"id": "observed", "title": "Engineer", "match_score": 90,
         "attributes": {"first_seen_at": RECENT}},
        {"id": "future", "title": "Engineer", "match_score": 90,
         "posted_at": (NOW + timedelta(days=1)).isoformat()},
    ]
    before = deepcopy(jobs)
    expected = {job["id"]: calculate_job_queue_priority(job, now=NOW) for job in jobs}
    for permutation in permutations(jobs):
        ranked = order_queue_by_fresh_priority(list(permutation), now=NOW)
        assert [row["id"] for row in ranked] == ["observed", "stale", "future"]
        for row in ranked:
            for key in ("timestamp_source", "timestamp_kind", "timestamp_state"):
                assert row[key] == expected[row["id"]][key]
            original = next(job for job in jobs if job["id"] == row["id"])
            assert row.get("posted_at") == original.get("posted_at")
            assert row.get("attributes") == original.get("attributes")
            assert row["queue_explanation"] == expected[row["id"]]["explanation"]
    assert jobs == before
