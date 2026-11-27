"""Step 53 requires retained same-application chronology, free of payload text."""

from copy import deepcopy

import pytest

from cli.acceptance_audit import application_audit_trace, validate_application_audit_trace


SCOPE = {
    "run_id": "acceptance-run-1", "source_commit": "a" * 40,
    "profile_id": "profile-b", "application_id": "application-b",
}
EVENTS = [
    {"id": "1" * 64, "type": "APPLICATION_STATUS_REPORTED", "index": 1,
     "application_id": "application-b", "timestamp": "2026-11-26T12:00:00.123456+00:00"},
    {"id": "f" * 32, "type": "APPLICATION_CONFIRMED", "index": 3,
     "application_id": "application-b", "timestamp": "2026-11-26T12:00:00.123455+00:00"},
    {"id": "4" * 64, "type": "APPLICATION_SUBMITTED", "index": 4,
     "application_id": "application-b", "timestamp": "2026-11-26T12:00:00.123454+00:00"},
]


def test_audit_trace_keeps_full_precision_original_indices_and_allowlisted_fields_only():
    source_events = [
        {**{key: value for key, value in event.items() if key != "index"},
         "subject": "private applicant name", "summary": "private status report",
         "answer": "private answer", "company": "private employer"}
        for event in EVENTS
    ]
    history = {"profile_id": SCOPE["profile_id"], "events": [
        {**source_events[0], "application_id": "app-a"},
        source_events[0],
        {"type": "PROFILE_UPDATED"},
        source_events[1], source_events[2],
    ]}
    trace = application_audit_trace(history, **SCOPE)
    assert trace == {**SCOPE, "events": EVENTS}
    assert "private" not in str(trace)
    with pytest.raises(ValueError, match="selected profile"):
        application_audit_trace({**history, "profile_id": "profile-a"}, **SCOPE)
    with pytest.raises(ValueError, match="three audit events"):
        application_audit_trace({**history, "events": history["events"] + [source_events[2]]}, **SCOPE)


def test_profile_audit_api_retains_order_and_precision_without_disclosing_payload(tmp_path):
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from packages.persistence import models
    from packages.persistence.store import Store

    store = Store(f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    try:
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        for profile_id in (SCOPE["profile_id"], "profile-a"):
            store.save_profile({"id": profile_id, "name": "Synthetic Profile",
                                "email": f"{profile_id}@example.invalid", "facts": [], "answers": []})
        actions = ("APPLICATION_PIPELINE_STATUS", "CONFIRMED", "APPLICATION_SUBMITTED")
        with store.engine.begin() as connection:
            for event, action in zip(reversed(EVENTS), reversed(actions)):
                connection.execute(models.audit.insert().values(
                    id=event["id"], entity_id=SCOPE["application_id"], action=action,
                    timestamp=event["timestamp"], payload={
                        "application_context": {
                            "profile_id": SCOPE["profile_id"], "application_id": SCOPE["application_id"],
                            "target": "MOCK",
                        },
                        "subject_snapshot": {"subject": "Bearer SYNTHETIC_PRIVATE_TOKEN", "company": ""},
                        "answer": "SYNTHETIC_PRIVATE_ANSWER",
                    }))
        app = create_app(store, "http://127.0.0.1:1", tmp_path, operation_mode="FIXTURE")
        with TestClient(app) as client:
            response = client.get(f"/profiles/{SCOPE['profile_id']}/audit-history")
            assert response.status_code == 200
            assert "SYNTHETIC_PRIVATE" not in response.text
            assert "application_context" not in response.text
            trace = application_audit_trace(response.json(), **SCOPE)
            assert [{key: value for key, value in row.items() if key != "index"}
                    for row in trace["events"]] == [
                {key: value for key, value in row.items() if key != "index"} for row in EVENTS]
            foreign = client.get("/profiles/profile-a/audit-history")
            assert foreign.status_code == 200
            assert not any(row.get("application_id") == SCOPE["application_id"]
                           for row in foreign.json()["events"])
    finally:
        store.engine.dispose()


@pytest.mark.parametrize("change", [
    "missing", "duplicate-id", "duplicate-type", "duplicate-index", "mixed-app",
    "tied-time", "reversed-time", "reversed-api-order", "reordered-events",
    "invalid-timestamp", "private-field", "wrong-profile", "wrong-run", "wrong-source",
])
def test_audit_trace_rejects_missing_or_invalid_retained_proof(change):
    trace = {**SCOPE, "events": deepcopy(EVENTS)}
    rows = trace["events"]
    if change == "missing":
        trace = None
    elif change == "duplicate-id":
        rows[1]["id"] = rows[0]["id"]
    elif change == "duplicate-type":
        rows[1]["type"] = rows[0]["type"]
    elif change == "duplicate-index":
        rows[1]["index"] = rows[0]["index"]
    elif change == "mixed-app":
        rows[1]["application_id"] = "app-a"
    elif change == "tied-time":
        rows[1]["timestamp"] = rows[0]["timestamp"]
    elif change == "reversed-time":
        rows[1]["timestamp"], rows[0]["timestamp"] = rows[0]["timestamp"], rows[1]["timestamp"]
    elif change == "reversed-api-order":
        rows[1]["index"], rows[0]["index"] = rows[0]["index"], rows[1]["index"]
    elif change == "reordered-events":
        rows.reverse()
    elif change == "invalid-timestamp":
        rows[0]["timestamp"] = "2026-02-31T12:00:00.123456+00:00"
    elif change == "private-field":
        rows[0]["summary"] = "private report text"
    elif change == "wrong-profile":
        trace["profile_id"] = "profile-a"
    elif change == "wrong-run":
        trace["run_id"] = "other-run"
    elif change == "wrong-source":
        trace["source_commit"] = "b" * 40
    with pytest.raises(ValueError, match="Criterion 53"):
        validate_application_audit_trace(trace, **SCOPE)
