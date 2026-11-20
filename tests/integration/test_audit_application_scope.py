"""Application audit attribution uses owned identities, never display labels."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.audit_history import record_mock_application_submitted
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.tracking.applications import set_application_pipeline_status
from tests.integration.test_spine import PROFILE


@pytest.fixture
def audit_case(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    store.save_profile(dict(PROFILE))
    store.save_profile({**PROFILE, "id": "other", "name": "Other Fictional Profile",
                        "email": "other@example.invalid"})
    with fixture_server() as origin:
        discover(store, origin, "fixture")
    with store.engine.begin() as connection:
        connection.execute(update(m.jobs).values(title="Same fictional title"))
        connection.execute(update(m.companies).values(name="Same fictional employer"))
    apps = [prepare(store, job["id"], "fixture", tmp_path) for job in store.jobs("fixture")]
    assert len(apps) == 2
    with TestClient(create_app(store, "http://127.0.0.1:1", tmp_path, operation_mode="FIXTURE")) as client:
        yield store, client, apps
    store.engine.dispose()


def _events(client, profile_id="fixture"):
    response = client.get(f"/profiles/{profile_id}/audit-history?limit=200")
    assert response.status_code == 200, response.text
    return response.json()["events"]


def _insert(store, event_id, entity_id, *, context=None, **payload):
    body = {"subject_snapshot": {"subject": "Same fictional title", "company": "Same fictional employer"},
            **payload}
    if context is not None:
        body["application_context"] = context
    with store.engine.begin() as connection:
        connection.execute(m.audit.insert().values(id=event_id, entity_id=entity_id,
            action="APPLICATION_SUBMITTED", payload=body, timestamp=now()))


def test_same_labels_do_not_conflate_application_audit_sequences(audit_case):
    store, client, apps = audit_case
    for app_id in apps:
        record_mock_application_submitted(store, app_id)
        with store.engine.begin() as connection:
            store._audit(connection, app_id, "CONFIRMED", {})
        set_application_pipeline_status(store, "fixture", app_id, "APPLIED", "tracking-" + app_id)
    events = _events(client)
    types = {"APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED", "APPLICATION_STATUS_REPORTED"}
    selected = [row for row in events if row["type"] in types]
    assert len(selected) == 6
    assert {(row["subject"], row["company"]) for row in selected} == {
        ("Same fictional title", "Same fictional employer")}
    for app_id in apps:
        assert {row["type"] for row in selected if row.get("application_id") == app_id} == types
    documents = [row for row in events if row["type"] in {"RESUME_GENERATED", "COVER_LETTER_GENERATED"}]
    assert {row.get("application_id") for row in documents} == set(apps)
    assert all(row.get("application_id") is None for row in events if row["type"] in {
        "PROFILE_UPDATED", "JOB_DISCOVERED", "MATCH_SCORED"})
    assert not any(row["type"] in types for row in _events(client, "other"))


def test_historical_context_and_owned_legacy_rows_keep_exact_application_identity(audit_case):
    store, client, apps = audit_case
    historical = "historical-owned-application"
    _insert(store, "historical-context", historical, context={
        "profile_id": "fixture", "application_id": historical, "target": "MOCK"})
    _insert(store, "owned-legacy", apps[0])
    _insert(store, "owned-legacy-tracking", identity("application-tracking", "fixture", apps[0]))
    _insert(store, "unowned-legacy", "missing-application")
    events = {row["id"]: row for row in _events(client)}
    assert events["historical-context"].get("application_id") == historical
    assert events["owned-legacy"].get("application_id") == apps[0]
    assert events["owned-legacy-tracking"].get("application_id") == apps[0]
    assert "unowned-legacy" not in events
    assert not any(row["id"] in {"historical-context", "owned-legacy", "owned-legacy-tracking"}
                   for row in _events(client, "other"))


def test_foreign_or_contradictory_context_is_omitted_and_private_payload_stays_redacted(audit_case):
    store, client, apps = audit_case
    _insert(store, "contradictory-owner", apps[0], context={
        "profile_id": "other", "application_id": apps[0], "target": "MOCK"})
    _insert(store, "contradictory-entity", apps[0], context={
        "profile_id": "fixture", "application_id": apps[1], "target": "MOCK"})
    _insert(store, "malformed-context", apps[0], context={
        "profile_id": "fixture", "application_id": apps[0], "target": "MOCK", "unexpected": "hidden"})
    _insert(store, "redacted-owned", apps[0], context={
        "profile_id": "fixture", "application_id": apps[0], "target": "MOCK"},
        subject_snapshot={"subject": "Bearer private-token-value", "company": "https://private.invalid"},
        answer="Private applicant answer", email="private-applicant@example.invalid")
    private_identity = "Bearer private-identity-value"
    _insert(store, "redacted-historical-identity", private_identity, context={
        "profile_id": "fixture", "application_id": private_identity, "target": "MOCK"})
    response = client.get("/profiles/fixture/audit-history?limit=200")
    events = {row["id"]: row for row in response.json()["events"]}
    assert not {"contradictory-owner", "contradictory-entity", "malformed-context"} & events.keys()
    assert events["redacted-owned"]["application_id"] == apps[0]
    assert events["redacted-owned"]["subject"] == "Details withheld"
    assert events["redacted-owned"]["company"] == ""
    assert events["redacted-historical-identity"].get("application_id") is None
    for private in ("private-token-value", "private.invalid", "Private applicant answer", "private-applicant@example.invalid"):
        assert private not in response.text
    assert "private-identity-value" not in response.text
    assert "contradictory-owner" not in {row["id"] for row in _events(client, "other")}
