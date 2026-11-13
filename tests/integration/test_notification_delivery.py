"""Real Store reads and durable delivery journal, with explicit PostgreSQL coverage."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, text, update

from cli.notifications import main
from packages.persistence import models as m
from packages.persistence import resume_workspace  # noqa: F401
from packages.persistence.store import Store
from services.notifications.configuration import parse_config
from services.notifications.contract import DeliveryResult, NotificationError
from services.notifications.events import event_from_row, read_event_page
from services.notifications.journal import DeliveryJournal
from services.notifications.providers.ntfy import NtfyProvider
from services.notifications.runner import run_once
from tests.unit.test_notification_configuration import config_data
from tests.unit.test_ntfy_provider import acknowledgement


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp()
    def __call__(self):
        return self.value
    def advance(self, seconds=10):
        self.value += seconds


def stamp(clock, offset=1):
    return datetime.fromtimestamp(clock() + offset, timezone.utc).isoformat()


def make_store(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'application.db').as_posix()}")
    store.migrate()
    store.save_profile({"id": "fixture", "name": "Synthetic Applicant", "email": "fixture@example.test",
                        "facts": [], "answers": []})
    store.save_profile({"id": "other", "name": "Other Applicant", "email": "other@example.test",
                        "facts": [], "answers": []})
    return store


def add_operation(store, clock, op="op-one", *, kind="DISCOVER", entity="fixture",
                  params=None, state="COMPLETED", attempts=1, at=None, completed_at=None):
    with store.engine.begin() as connection:
        connection.execute(m.operations.insert().values(
            id=op, kind=kind, entity_id=entity,
            params={} if params is None else params, state=state,
            error="PRIVATE_ERROR_DO_NOT_EXPORT", updated_at=at or stamp(clock),
            completed_at=completed_at if completed_at is not None else at or stamp(clock),
            attempt_count=attempts,
        ))


def add_ownership_entities(store, clock):
    with store.engine.begin() as connection:
        connection.execute(m.companies.insert().values(id="company", name="Private Company", domain=""))
        connection.execute(m.jobs.insert().values(
            id="job", company_id="company", requisition="private-job", title="PRIVATE_JOB_TITLE",
            description="PRIVATE_JOB_BODY", skills=[], location="Remote", remote_type="REMOTE", attributes={}))
        connection.execute(m.routes.insert().values(
            id="route", job_id="job", company_id="company", provider="mock-ats", tenant="fixture",
            requisition="private-job", url="https://example.invalid/apply"))
        for profile, app, resume in (
                ("fixture", "app-owned", "resume-owned"), ("other", "app-other", "resume-other")):
            connection.execute(m.applications.insert().values(
                id=app, job_id="job", route_id="route", profile_id=profile, profile_version="fixture-v1",
                state="PREPARED", target="MOCK", fact_ids=[], trace={}, created_at=stamp(clock)))
            connection.execute(m.uploaded_resumes.insert().values(
                id=resume, profile_id=profile, filename="PRIVATE_RESUME.pdf", format="pdf",
                media_type="application/pdf", size_bytes=1, sha256="a" * 64,
                status="UPLOADED", created_at=stamp(clock)))


@contextmanager
def ownership_store(tmp_path, backend):
    if backend == "sqlite":
        store = make_store(tmp_path)
        try:
            yield store
        finally:
            store.engine.dispose()
        return
    raw = os.environ.get("MJOBS_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("NOT_RUN: disposable loopback MJOBS_TEST_POSTGRES_URL absent")
    from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
    url = _disposable_postgres_url(raw)
    schema = "mjobs_notification_" + uuid4().hex
    admin = create_engine(url, connect_args={"connect_timeout": 5}, hide_parameters=True)
    store = None
    try:
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        scoped = url.update_query_dict({
            "options": "-csearch_path=" + schema + " -clock_timeout=5000 -cstatement_timeout=30000",
        })
        store = Store(scoped.render_as_string(hide_password=False))
        m.metadata.create_all(store.engine)
        for profile in ("fixture", "other"):
            store.save_profile({"id": profile, "name": "Synthetic Applicant",
                                "email": profile + "@example.test", "facts": [], "answers": []})
        yield store
    finally:
        if store is not None:
            store.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_reader_requires_kind_and_exact_profile_application_resume_ownership(tmp_path, backend):
    clock = Clock()
    with ownership_store(tmp_path, backend) as store:
        assert store.engine.dialect.name == ("postgresql" if backend == "postgres" else "sqlite")
        add_ownership_entities(store, clock)
        add_operation(store, clock, "profile-good")
        add_operation(store, clock, "app-good", kind="APPLICATION_DRY_RUN", entity="app-owned",
                      params={"profile_id": "fixture", "private": "DO_NOT_EXPORT"})
        add_operation(store, clock, "resume-good", kind="RESUME_EXTRACT_QUEUED", entity="resume-owned",
                      params={"profile_id": "fixture"})
        for op, kind, entity, params in (
            ("foreign-profile", "DISCOVER", "other", {}),
            ("conflicting-profile", "DISCOVER", "fixture", {"profile_id": "other"}),
            ("unknown-kind", "UNKNOWN", "fixture", {}),
            ("source-wide", "SOURCE_COLLECT", "fixture", {"profile_id": "fixture"}),
            ("foreign-app", "APPLICATION_DRY_RUN", "app-other", {"profile_id": "fixture"}),
            ("conflicting-app", "APPLICATION_DRY_RUN", "app-owned", {"profile_id": "other"}),
            ("unbound-app", "APPLICATION_DRY_RUN", "app-owned", {}),
            ("wrong-kind-app", "DISCOVER", "app-owned", {}),
            ("foreign-resume", "RESUME_EXTRACT_QUEUED", "resume-other", {"profile_id": "fixture"}),
            ("unbound-resume", "RESUME_EXTRACT_QUEUED", "resume-owned", {}),
            ("wrong-kind-resume", "APPLICATION_DRY_RUN", "resume-owned", {"profile_id": "fixture"}),
        ):
            add_operation(store, clock, op, kind=kind, entity=entity, params=params)
        events, cursor = read_event_page(store, "fixture", activated_at=stamp(clock, 0))
        assert {event.operation_id for event in events} == {"profile-good", "app-good", "resume-good"}
        assert cursor == ""
        public = " ".join(event.message for event in events)
        for private in ("PRIVATE", "DO_NOT_EXPORT", "Synthetic Applicant", "Private Company", "submitted"):
            assert private not in public
        with pytest.raises(NotificationError, match="NOTIFICATION_PROFILE_NOT_FOUND"):
            read_event_page(store, "missing", activated_at=stamp(clock, 0))


def test_actual_collector_to_provider_ack_survives_reopen_and_metadata_touch(tmp_path):
    clock = Clock()
    store = make_store(tmp_path)
    config = parse_config(config_data(tmp_path))
    journal = DeliveryJournal(config, clock=clock)
    requests = []
    def handler(request):
        requests.append(request.content)
        return httpx.Response(200, json=acknowledgement(request))
    sender = NtfyProvider(config, transport=httpx.MockTransport(handler))
    try:
        add_operation(store, clock)
        assert run_once(store, config, journal, sender)["attempted"] == 1
        assert journal.status()["counts"] == {"DELIVERED": 1}
        clock.advance()
        with store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == "op-one").values(
                updated_at=stamp(clock), error="CHANGED_PRIVATE_METADATA"))
        reopened = DeliveryJournal(config, clock=clock)
        assert run_once(store, config, reopened, sender)["attempted"] == 0
        assert len(requests) == 1
        # A new actual worker attempt has a distinct outcome identity.
        with store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == "op-one").values(
                attempt_count=2, completed_at=stamp(clock)))
        assert run_once(store, config, reopened, sender)["attempted"] == 1
        assert len(requests) == 2
        assert all(b"PRIVATE" not in item for item in requests)
    finally:
        store.engine.dispose()


def test_activation_excludes_old_outcomes_even_when_metadata_changes(tmp_path):
    clock = Clock()
    store = make_store(tmp_path)
    try:
        add_operation(store, clock, "old", at=stamp(clock, -10))
        config = parse_config(config_data(tmp_path))
        journal = DeliveryJournal(config, clock=clock)
        with store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == "old").values(
                updated_at=stamp(clock, 10)))
        class Forbidden:
            def send(self, event):
                raise AssertionError("Historical outcome must not send")
        result = run_once(store, config, journal, Forbidden())
        assert result["queued"] == 0 and result["attempted"] == 0
    finally:
        store.engine.dispose()


def test_bounded_sweeps_eventually_visit_every_event_and_dedupe_revisits(tmp_path):
    clock = Clock()
    store = make_store(tmp_path)
    config = parse_config(config_data(tmp_path, batch_size=2))
    journal = DeliveryJournal(config, clock=clock)
    calls = []
    class Sender:
        def send(self, event):
            calls.append(event.event_key)
            return DeliveryResult("DELIVERED", "NOTIFICATION_SERVER_ACKNOWLEDGED", "synthetic")
    try:
        for number in range(5):
            add_operation(store, clock, "op-" + str(number))
        for _ in range(10):
            run_once(store, config, journal, Sender())
            clock.advance()
        assert len(calls) == 5 and len(set(calls)) == 5
        assert journal.status()["counts"] == {"DELIVERED": 5}
    finally:
        store.engine.dispose()


def test_concurrent_reservation_allows_one_send_and_token_bound_completion(tmp_path):
    clock = Clock()
    config = parse_config(config_data(tmp_path))
    journal = DeliveryJournal(config, clock=clock)
    event = event_from_row({"id": "one", "kind": "DISCOVER", "state": "COMPLETED",
                            "updated_at": stamp(clock), "attempt_count": 1})
    journal.enqueue_page([event], expected_cursor="", next_cursor="")
    def reserve(_):
        return DeliveryJournal(config, clock=clock).claim_next()
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(reserve, range(4)))
    assert sum(claim is not None for claim in claims) == 1
    owned_event, token = next(claim for claim in claims if claim is not None)
    with pytest.raises(NotificationError, match="NOTIFICATION_DELIVERY_OWNERSHIP_LOST"):
        journal.finish(owned_event.event_key, "wrong-token",
                       DeliveryResult("DELIVERED", "NOTIFICATION_SERVER_ACKNOWLEDGED", "wrong"))
    journal.finish(owned_event.event_key, token, DeliveryResult(
        "DELIVERED", "NOTIFICATION_SERVER_ACKNOWLEDGED", "right"))
    assert journal.status()["counts"] == {"DELIVERED": 1}


def test_interrupted_or_uncertain_send_is_never_replayed_or_retryable(tmp_path):
    clock = Clock()
    config = parse_config(config_data(tmp_path))
    journal = DeliveryJournal(config, clock=clock)
    event = event_from_row({"id": "one", "kind": "DISCOVER", "state": "FAILED",
                            "updated_at": stamp(clock), "attempt_count": 1})
    journal.enqueue_page([event], expected_cursor="", next_cursor="")
    _, token = journal.claim_next()
    clock.advance(100)
    restarted = DeliveryJournal(config, clock=clock)
    assert restarted.claim_next() is None
    status = restarted.status()
    assert status["counts"] == {"UNKNOWN": 1}
    assert status["deliveries"][0]["status"] == "UNKNOWN"
    with pytest.raises(NotificationError, match="NOTIFICATION_RETRY_UNSAFE"):
        restarted.retry(event.event_key)
    # A late acknowledgement from the exact original owner can still reconcile.
    journal.finish(event.event_key, token, DeliveryResult(
        "DELIVERED", "NOTIFICATION_SERVER_ACKNOWLEDGED", "late"))
    assert restarted.status()["counts"] == {"DELIVERED": 1}


def test_rate_limit_blocks_all_new_sends_and_requires_bounded_explicit_retry(tmp_path):
    clock = Clock()
    config = parse_config(config_data(tmp_path))
    journal = DeliveryJournal(config, clock=clock)
    events = [event_from_row({"id": str(number), "kind": "DISCOVER", "state": "FAILED",
                             "updated_at": stamp(clock), "attempt_count": 1}) for number in range(2)]
    journal.enqueue_page(events, expected_cursor="", next_cursor="")
    event, token = journal.claim_next()
    journal.finish(event.event_key, token, DeliveryResult(
        "REJECTED", "NOTIFICATION_RATE_LIMITED", retry_after=120))
    assert journal.claim_next() is None
    with pytest.raises(NotificationError, match="NOTIFICATION_RETRY_NOT_DUE"):
        journal.retry(event.event_key)
    clock.advance(120)
    # No implicit retry: the other pending event is the only eligible one.
    other, other_token = journal.claim_next()
    assert other.event_key != event.event_key
    journal.finish(other.event_key, other_token, DeliveryResult(
        "DELIVERED", "NOTIFICATION_SERVER_ACKNOWLEDGED", "other"))
    for _ in range(2):
        clock.advance()
        journal.retry(event.event_key)
        retried, retry_token = journal.claim_next()
        assert retried.event_key == event.event_key
        journal.finish(event.event_key, retry_token, DeliveryResult(
            "REJECTED", "NOTIFICATION_AUTH_REJECTED"))
    with pytest.raises(NotificationError, match="NOTIFICATION_RETRY_EXHAUSTED"):
        journal.retry(event.event_key)


def test_destination_and_profile_changes_cannot_reinterpret_journal(tmp_path):
    config = parse_config(config_data(tmp_path))
    DeliveryJournal(config)
    for changed in (replace(config, topic="different"), replace(config, profile_id="other"),
                    replace(config, server_url="https://other.example.test")):
        with pytest.raises(NotificationError, match="NOTIFICATION_STATE_BINDING_MISMATCH"):
            DeliveryJournal(changed)
    assert DeliveryJournal(replace(config, token_env="NEW_TOKEN")).status()["counts"] == {}


def test_cli_status_is_private_and_retry_has_no_network_side_effect(tmp_path, capsys):
    clock = Clock()
    data = config_data(tmp_path, topic="SECRET_TOPIC", token_env="SECRET_ENV")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    config = parse_config(data)
    journal = DeliveryJournal(config, clock=clock)
    event = event_from_row({"id": "PRIVATE_OP", "kind": "DISCOVER", "state": "FAILED",
                            "updated_at": stamp(clock), "attempt_count": 1})
    journal.enqueue_page([event], expected_cursor="", next_cursor="")
    _, token = journal.claim_next()
    journal.finish(event.event_key, token, DeliveryResult("REJECTED", "NOTIFICATION_AUTH_REJECTED"))
    assert main(["--config", str(path), "status"]) == 0
    output = capsys.readouterr().out
    assert "REJECTED" in output
    for secret in ("SECRET_TOPIC", "SECRET_ENV", "PRIVATE_OP", "notifications.example.test"):
        assert secret not in output
    assert main(["--config", str(path), "retry", event.event_key]) == 0
    assert "RETRY_QUEUED" in capsys.readouterr().out
    assert journal.status()["counts"] == {"PENDING": 1}
