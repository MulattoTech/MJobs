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


def test_safe_report_preserves_only_valid_dynamic_observation():
    from tests.unit.test_fleet_t02_workday_dynamic_contract import project
    report = {**worker_fixture.SAFE_ANALYSIS, "dynamic_review": project()}
    assert _safe_report(report) == report
    report["dynamic_review"]["fields"][0]["value"] = "SYNTHETIC_PRIVATE_VALUE"
    with pytest.raises(FormReviewError, match="FORM_REVIEW_ANALYSIS_INVALID"):
        _safe_report(report)
