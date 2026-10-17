"""Synthetic provider review through API, worker, and reopened durable storage."""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from tests.integration import test_application_form_review_flow as flow
from workers.tasks import execute_operation


@pytest.fixture
def review_flow():
    fixture = flow.ApplicationFormReviewFlowTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


@pytest.mark.parametrize("provider,tenant,requisition,url", [
    ("ashby", "fixture", "engineering-123", "https://jobs.ashbyhq.com/fixture/engineering-123"),
    ("lever", "fixture", "01234567-89ab-cdef-0123-456789abcdef",
     "https://jobs.lever.co/fixture/01234567-89ab-cdef-0123-456789abcdef"),
])
@pytest.mark.parametrize("complete", [True, False])
def test_provider_review_readback_preserves_complete_or_interrupted_form(
        review_flow, provider, tenant, requisition, url, complete):
    fixture = review_flow
    route_id = fixture.store.application(fixture.application_id)["route_id"]
    with fixture.store.engine.begin() as connection:
        connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
            provider=provider, tenant=tenant, requisition=requisition, url=url,
        ))
    path = f"/profiles/fixture/applications/{fixture.application_id}/form-reviews"
    request_id = "provider-complete" if complete else "provider-interrupted"
    queued = fixture.client.post(path, json={"request_id": request_id})
    assert queued.status_code == 202, queued.text
    operation_id = queued.json()["operation_id"]
    html = ('<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" required value="PRIVATE_VALUE">')
    if complete:
        html += "</form>"

    class Response:
        headers = {"content-type": "text/html; charset=utf-8"}
        content = html.encode()

    class SyntheticPublicClient:
        def __init__(self, website, check=None, total_timeout=None):
            assert website == url
            self.check = check

        def get(self, requested_url):
            assert requested_url == url
            self.check()
            response = Response()
            response.url = url
            return response

        def allowed(self, requested_url):
            return requested_url == url

        def close(self):
            pass

    with patch.dict("os.environ", {
        "MJOBS_OPERATION_MODE": "FIXTURE",
        "MJOBS_DATABASE_URL": fixture.database_url,
        "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
        "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
        "MJOBS_OUTPUT_DIR": str(fixture.root / "documents"),
    }), patch("workers.tasks.PublicCompanyClient", SyntheticPublicClient):
        assert execute_operation(operation_id)["status"] == "COMPLETED"
        # Completed operations cannot be reclaimed to overwrite their review.
        assert execute_operation(operation_id)["status"] == "ALREADY_CLAIMED_OR_CAPACITY"

    expected = ({
        "status": "ANALYZED", "reason": None,
        "fields": [{"id": "email", "name": "email", "type": "email",
                    "required": True, "review_reason": None}],
    } if complete else {
        "status": "REVIEW_REQUIRED", "reason": "MARKUP_PARSE_FAILED", "fields": [],
    })
    fixture.client.close()
    fixture.store.engine.dispose()
    fixture.store = fixture._new_store()
    fixture.client = TestClient(create_app(
        fixture.store, "http://127.0.0.1:1", fixture.root,
        "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
    ))
    for suffix in ("/" + request_id, "/latest"):
        response = fixture.client.get(path + suffix)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "COMPLETED"
        assert response.json()["analysis"] == expected
        assert "PRIVATE_VALUE" not in response.text
    with fixture.store.engine.connect() as connection:
        audits = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == fixture.application_id,
            m.audit.c.action == "APPLICATION_FORM_REVIEW",
        )).scalars().all()
    assert len(audits) == 1
    assert audits[0]["analysis"] == expected
    assert "PRIVATE_VALUE" not in json.dumps(audits)
    assert fixture.store.count("application_attempts") == 0
