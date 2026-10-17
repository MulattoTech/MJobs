"""Review admission order and profile snapshot are durable user intent."""
from unittest.mock import Mock

import pytest
from sqlalchemy import update

from packages.persistence import models as m
from tests.integration import test_application_form_review_flow as flow_fixture
from tests.unit import test_application_form_review_worker as worker_fixture
from workers.application_form_review import FormReviewError, handle_form_review


def test_latest_uses_admission_order_even_after_older_operation_changes():
    fixture = flow_fixture.ApplicationFormReviewFlowTests()
    fixture.setUp()
    try:
        path = fixture._complete_review("older-request")
        old = fixture.client.post(path, json={"request_id": "older-request"}).json()
        new = fixture.client.post(path, json={"request_id": "newer-request"})
        assert new.status_code == 202
        with fixture.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == old["operation_id"],
            ).values(updated_at="2999-01-01T00:00:00+00:00"))
        latest = fixture.client.get(path + "/latest").json()
        assert latest["request_id"] == "newer-request"
        assert latest["state"] == "QUEUED"
        assert latest["analysis"] is None
        assert fixture.client.get(path + "/older-request").json()["analysis"] is not None
        assert fixture.client.post(path, json={"request_id": "newer-request"}).json() == new.json()
    finally:
        fixture.tearDown()


@pytest.mark.parametrize("during_fetch", [False, True])
def test_profile_change_prevents_new_receipt(during_fetch):
    fixture = worker_fixture.ApplicationFormReviewWorkerTests()
    fixture.setUp()
    try:
        operation = fixture.make_operation(profile_version=str(fixture.store.profile("fixture")["version"]))

        def change_profile():
            profile = fixture.store.profile("fixture")
            profile["name"] = "Synthetic Revised Profile"
            fixture.store.save_profile(profile)

        def fetch(_):
            change_profile()
            return "<form/>"

        fetch_html = Mock(side_effect=fetch) if during_fetch else Mock(return_value="<form/>")
        if not during_fetch:
            change_profile()
        service, _ = fixture.with_analysis_service()
        with service, pytest.raises(FormReviewError, match="FORM_REVIEW_INPUT_STALE"):
            handle_form_review(fixture.store, operation, fetch_html=fetch_html, check=lambda: None)
        if not during_fetch:
            fetch_html.assert_not_called()
        assert fixture.audit_events(operation["id"]) == []
    finally:
        fixture.tearDown()


def test_registered_provider_reaches_public_admission_worker_and_readback():
    fixture = flow_fixture.ApplicationFormReviewFlowTests()
    fixture.setUp()
    try:
        route_id = fixture.store.application(fixture.application_id)["route_id"]
        fixture.route_url = "https://example.recruitee.com/o/engineer"
        with fixture.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                provider="recruitee", tenant="example", requisition="engineer", url=fixture.route_url,
            ))
        path = fixture._complete_review("registered-provider")
        exact = fixture.client.get(path + "/registered-provider")
        assert exact.status_code == 200
        assert exact.json()["state"] == "COMPLETED"
        assert exact.json()["analysis"] is not None
        assert fixture.client.get(path + "/latest").json() == exact.json()
    finally:
        fixture.tearDown()
