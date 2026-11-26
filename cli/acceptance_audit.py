"""Redacted chronology for the selected mock application's acceptance step."""

from datetime import datetime
import re


_NEWEST_FIRST_TYPES = (
    "APPLICATION_STATUS_REPORTED",
    "APPLICATION_CONFIRMED",
    "APPLICATION_SUBMITTED",
)
_TRACE_KEYS = {"run_id", "source_commit", "profile_id", "application_id", "events"}
_EVENT_KEYS = {"id", "type", "timestamp", "application_id", "index"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_EVENT_ID = re.compile(r"(?:[a-f0-9]{32}|[a-f0-9]{64})")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00")


def validate_application_audit_trace(trace, *, run_id, source_commit, profile_id, application_id):
    """Require distinct events with strictly decreasing times and increasing API indices."""
    if (not isinstance(trace, dict) or set(trace) != _TRACE_KEYS
            or any(not isinstance(value, str) or not _ID.fullmatch(value)
                   for value in (run_id, profile_id, application_id))
            or not isinstance(source_commit, str) or not re.fullmatch(r"[a-f0-9]{40}", source_commit)
            or trace["run_id"] != run_id or trace["source_commit"] != source_commit
            or trace["profile_id"] != profile_id or trace["application_id"] != application_id):
        raise ValueError("Criterion 53 requires a source-bound application audit trace")
    events = trace["events"]
    if not isinstance(events, list) or len(events) != len(_NEWEST_FIRST_TYPES):
        raise ValueError("Criterion 53 requires exactly three audit events")
    timestamps = []
    indices = []
    event_ids = set()
    for event, kind in zip(events, _NEWEST_FIRST_TYPES):
        if (not isinstance(event, dict) or set(event) != _EVENT_KEYS
                or event["type"] != kind or event["application_id"] != application_id
                or not isinstance(event["id"], str) or not _EVENT_ID.fullmatch(event["id"])
                or event["id"] in event_ids or type(event["index"]) is not int
                or not 0 <= event["index"] < 100
                or not isinstance(event["timestamp"], str) or not _TIMESTAMP.fullmatch(event["timestamp"])):
            raise ValueError("Criterion 53 has an invalid, duplicated or reordered audit event")
        try:
            parsed = datetime.fromisoformat(event["timestamp"])
        except ValueError:
            raise ValueError("Criterion 53 has an invalid audit timestamp") from None
        timestamps.append(parsed)
        indices.append(event["index"])
        event_ids.add(event["id"])
    if not (timestamps[0] > timestamps[1] > timestamps[2] and indices[0] < indices[1] < indices[2]):
        raise ValueError("Criterion 53 audit chronology does not match newest-first API order")


def application_audit_trace(history, *, run_id, source_commit, profile_id, application_id):
    """Project only the same-application proof from the profile-scoped API response."""
    if (not isinstance(history, dict) or history.get("profile_id") != profile_id
            or not isinstance(history.get("events"), list) or len(history["events"]) > 100
            or any(not isinstance(event, dict) for event in history["events"])):
        raise ValueError("Criterion 53 requires the selected profile's bounded audit response")
    events = [
        {**{key: event.get(key) for key in _EVENT_KEYS - {"index"}}, "index": index}
        for index, event in enumerate(history["events"])
        if event.get("application_id") == application_id and event.get("type") in _NEWEST_FIRST_TYPES
    ]
    trace = {
        "run_id": run_id, "source_commit": source_commit, "profile_id": profile_id,
        "application_id": application_id, "events": events,
    }
    validate_application_audit_trace(
        trace, run_id=run_id, source_commit=source_commit, profile_id=profile_id,
        application_id=application_id)
    return trace
