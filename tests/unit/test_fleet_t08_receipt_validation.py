"""Persisted review receipts must retain the worker's value exclusion boundary."""
from unittest.mock import Mock

import pytest
from sqlalchemy import update

from packages.persistence import models as m
from tests.unit import test_application_form_review_worker as worker_fixture
from tests.integration import test_application_form_review_flow as flow_fixture
from workers.application_form_review import FormReviewError, handle_form_review, _safe_report


@pytest.fixture
def review():
    fixture = worker_fixture.ApplicationFormReviewWorkerTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def test_replay_rejects_corrupt_receipt_without_refetch(review):
    operation = review.make_operation()
    service, _ = review.with_analysis_service()
    with service:
        handle_form_review(review.store, operation, fetch_html=lambda _: "<form/>", check=lambda: None)
    event = review.audit_events(operation["id"])[0]
    payload = event["payload"]
    payload["analysis"]["private_answer"] = "SYNTHETIC_PRIVATE_VALUE"
    with review.store.engine.begin() as connection:
        connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(payload=payload))
    fetch = Mock(side_effect=AssertionError("must not refetch corrupt receipt"))
    with pytest.raises(FormReviewError, match="FORM_REVIEW_ANALYSIS_INVALID"):
        handle_form_review(review.store, operation, fetch_html=fetch, check=lambda: None)
    fetch.assert_not_called()


def test_exact_readback_rejects_corrupt_receipt_without_exposing_values():
    fixture = flow_fixture.ApplicationFormReviewFlowTests()
    fixture.setUp()
    try:
        path = fixture._complete_review("receipt-corrupt")
        with fixture.store.engine.begin() as connection:
            event = connection.execute(m.audit.select().where(
                m.audit.c.entity_id == fixture.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings().one()
            payload = event["payload"]
            payload["analysis"]["private_answer"] = "SYNTHETIC_PRIVATE_VALUE"
            connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(payload=payload))
        response = fixture.client.get(path + "/receipt-corrupt")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "FORM_REVIEW_ANALYSIS_INVALID"
        assert "SYNTHETIC_PRIVATE_VALUE" not in response.text
    finally:
        fixture.tearDown()


def dynamic_report():
    from tests.unit.test_fleet_t02_workday_dynamic_contract import project
    envelope = project()
    return {"status": "REVIEW_REQUIRED", "reason": "WORKDAY_DYNAMIC_FIELD_PRESENT",
            "fields": [{**{key: field[key] for key in ("id", "name", "type", "required")},
                        "review_reason": "UNKNOWN_QUESTION"} for field in envelope["fields"]],
            "dynamic_review": envelope}


def test_safe_report_preserves_only_valid_dynamic_observation():
    report = dynamic_report()
    assert _safe_report(report, provider="workday") == report
    report["dynamic_review"]["fields"][0]["value"] = "SYNTHETIC_PRIVATE_VALUE"
    with pytest.raises(FormReviewError, match="FORM_REVIEW_ANALYSIS_INVALID"):
        _safe_report(report, provider="workday")


@pytest.mark.parametrize("change", ["order", "id", "name", "type", "required", "status"])
def test_dynamic_envelope_cannot_describe_different_outer_observation(change):
    report = dynamic_report()
    if change == "status":
        report["status"] = "ANALYZED"
    elif change == "order":
        report["fields"].reverse()
    elif change == "required":
        report["fields"][0]["required"] = not report["fields"][0]["required"]
    else:
        report["fields"][0][change] = "text" if change == "type" else "changed"
    with pytest.raises(FormReviewError, match="FORM_REVIEW_ANALYSIS_INVALID"):
        _safe_report(report, provider="workday")


def test_non_workday_worker_cannot_persist_workday_dynamic_observation(review):
    operation = review.make_operation()
    service, _ = review.with_analysis_service(dynamic_report())
    with service, pytest.raises(FormReviewError, match="FORM_REVIEW_ANALYSIS_INVALID"):
        handle_form_review(review.store, operation, fetch_html=lambda _: "<form/>", check=lambda: None)
    assert review.audit_events(operation["id"]) == []


def test_non_workday_receipt_cannot_replay_workday_dynamic_observation(review):
    operation = review.make_operation()
    service, _ = review.with_analysis_service()
    with service:
        handle_form_review(review.store, operation, fetch_html=lambda _: "<form/>", check=lambda: None)
    receipt = review.audit_events(operation["id"])[0]
    payload = receipt["payload"]
    payload["analysis"] = dynamic_report()
    with review.store.engine.begin() as connection:
        connection.execute(update(m.audit).where(m.audit.c.id == receipt["id"]).values(payload=payload))
    with pytest.raises(FormReviewError, match="FORM_REVIEW_ANALYSIS_INVALID"):
        handle_form_review(review.store, operation, fetch_html=lambda _: pytest.fail("must not fetch"), check=lambda: None)


@pytest.mark.parametrize("key,value", [("status", []), ("type", []), ("review_reason", {})])
def test_corrupt_receipt_scalar_types_use_safe_error(key, value):
    from copy import deepcopy
    report = deepcopy(worker_fixture.SAFE_ANALYSIS)
    if key == "status":
        report[key] = value
    else:
        report["fields"][0][key] = value
    with pytest.raises(FormReviewError, match="FORM_REVIEW_ANALYSIS_INVALID"):
        _safe_report(report)


@pytest.mark.parametrize("corrupt", [False, True])
def test_recovery_receipt_probe_accepts_only_valid_saved_result(review, corrupt):
    from workers import application_form_review as worker
    operation = review.make_operation()
    service, _ = review.with_analysis_service()
    with service:
        handle_form_review(review.store, operation, fetch_html=lambda _: "<form/>", check=lambda: None)
    if corrupt:
        event = review.audit_events(operation["id"])[0]
        payload = event["payload"]
        payload["analysis"]["private_answer"] = "SYNTHETIC_PRIVATE_VALUE"
        with review.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(payload=payload))
    with review.store.engine.connect() as connection:
        assert worker.form_review_saved(connection, operation) is (not corrupt)


@pytest.mark.parametrize("key", ["profile_id", "route_id", "provider"])
def test_replay_rejects_receipt_from_wrong_frozen_scope(review, key):
    operation = review.make_operation()
    service, _ = review.with_analysis_service()
    with service:
        handle_form_review(review.store, operation, fetch_html=lambda _: "<form/>", check=lambda: None)
    receipt = review.audit_events(operation["id"])[0]
    payload = receipt["payload"]
    payload[key] = "wrong-synthetic-scope"
    with review.store.engine.begin() as connection:
        connection.execute(update(m.audit).where(m.audit.c.id == receipt["id"]).values(payload=payload))
    with pytest.raises(FormReviewError, match="FORM_REVIEW_IDEMPOTENCY_CONFLICT"):
        handle_form_review(review.store, operation, fetch_html=lambda _: pytest.fail("must not fetch"), check=lambda: None)
