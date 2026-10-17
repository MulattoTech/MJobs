"""A committed receipt survives the final-attempt worker interruption."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from tests.integration import test_application_form_review_flow as flow_fixture
from workers.operations import MAX_ATTEMPTS, recover_expired


@pytest.mark.parametrize("corrupt", [False, True])
def test_final_attempt_receipt_reconciliation_after_reopen(corrupt):
    fixture = flow_fixture.ApplicationFormReviewFlowTests()
    fixture.setUp()
    try:
        path = fixture._complete_review("final-attempt")
        original = fixture.client.get(path + "/final-attempt").json()
        with fixture.store.engine.begin() as connection:
            operation_id = connection.execute(select(m.operations.c.id).where(
                m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                m.operations.c.entity_id == fixture.application_id,
            )).scalar_one()
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                state="RUNNING", attempt_count=MAX_ATTEMPTS, completed_at=None,
                progress=20, current_step="PREPARING", lease_expires_at="2000-01-01T00:00:00+00:00",
            ))
            if corrupt:
                receipt = connection.execute(select(m.audit).where(
                    m.audit.c.entity_id == fixture.application_id,
                    m.audit.c.action == "APPLICATION_FORM_REVIEW",
                )).mappings().one()
                payload = receipt["payload"]
                payload["analysis"]["private_answer"] = "SYNTHETIC_PRIVATE_VALUE"
                connection.execute(update(m.audit).where(m.audit.c.id == receipt["id"]).values(payload=payload))

        fixture.client.close()
        fixture.store.engine.dispose()
        fixture.store = fixture._new_store()
        fixture.client = TestClient(create_app(
            fixture.store, "http://127.0.0.1:1", fixture.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))
        with patch("workers.tasks.PublicCompanyClient", side_effect=AssertionError("must not fetch")) as fetch:
            assert recover_expired(fixture.store) == 1
        fetch.assert_not_called()
        with fixture.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one()
            receipts = list(connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == fixture.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).scalars())
        assert len(receipts) == 1
        exact = fixture.client.get(path + "/final-attempt")
        latest = fixture.client.get(path + "/latest")
        if corrupt:
            assert operation["state"] == "DEAD_LETTERED"
            for response in (exact, latest):
                assert response.status_code == 409
                assert "SYNTHETIC_PRIVATE_VALUE" not in response.text
        else:
            assert operation["state"] == "COMPLETED"
            assert operation["error"] is None
            assert operation["progress"] == 100
            assert operation["current_step"] == "COMPLETED"
            assert operation["completed_at"] is not None
            assert exact.status_code == latest.status_code == 200
            assert exact.json() == latest.json() == original
    finally:
        fixture.tearDown()
