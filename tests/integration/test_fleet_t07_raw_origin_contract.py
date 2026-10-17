"""Actual supported public-route raw-origin contract for the shared guard.

These regressions intentionally fail on the unguarded ef6b629f source. They
are consumed by the common-boundary owner without changing adapter ownership.
"""
import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from tests.integration import test_application_form_review_flow as flow


LEVER_JOB = "01234567-89ab-cdef-0123-456789abcdef"
ROUTES = [
    ("greenhouse", "123", "https://BOARDS.GREENHOUSE.IO/Example/jobs/123",
     "https://board\u017f.greenhouse.io/example/jobs/123",
     "https://boards.greenhouse.io/example/jobs/123"),
    ("greenhouse", "123", "https://JOB-BOARDS.GREENHOUSE.IO/Example/jobs/123",
     "https://job-board\u017f.greenhouse.io/example/jobs/123",
     "https://boards.greenhouse.io/example/jobs/123"),
    ("lever", LEVER_JOB, f"https://JOBS.LEVER.CO/Example/{LEVER_JOB.upper()}",
     f"https://job\u017f.lever.co/example/{LEVER_JOB}",
     f"https://jobs.lever.co/example/{LEVER_JOB}"),
    ("lever", LEVER_JOB, f"https://JOBS.EU.LEVER.CO/Example/{LEVER_JOB.upper()}",
     f"https://job\u017f.eu.lever.co/example/{LEVER_JOB}",
     f"https://jobs.eu.lever.co/example/{LEVER_JOB}"),
    ("ashby", "engineering-123", "https://JOBS.ASHBYHQ.COM/Example/Engineering-123",
     "https://job\u017f.ashbyhq.com/example/engineering-123",
     "https://jobs.ashbyhq.com/example/engineering-123"),
]


@pytest.fixture
def review_flow():
    fixture = flow.ApplicationFormReviewFlowTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


@pytest.mark.parametrize("provider,requisition,ascii_url,unicode_url,canonical", ROUTES)
@pytest.mark.parametrize("ambiguous", [False, True], ids=["ascii-control", "unicode-host"])
def test_supported_public_route_preserves_raw_origin_identity(
        review_flow, provider, requisition, ascii_url, unicode_url, canonical, ambiguous):
    fixture = review_flow
    raw_url = unicode_url if ambiguous else ascii_url
    route_id = fixture.store.application(fixture.application_id)["route_id"]
    with fixture.store.engine.begin() as connection:
        connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
            provider=provider, tenant="example", requisition=requisition, url=raw_url,
        ))
    before = {table.name: fixture.store.count(table.name) for table in (
        m.operations, m.audit, m.attempts,
    )}
    path = f"/profiles/fixture/applications/{fixture.application_id}/form-reviews"

    response = fixture.client.post(path, json={"request_id": "raw-origin-contract"})

    if ambiguous:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "FORM_REVIEW_ROUTE_UNSUPPORTED"
        assert {name: fixture.store.count(name) for name in before} == before
    else:
        assert response.status_code == 202, response.text
        with fixture.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == response.json()["operation_id"],
            )).mappings().one()
        assert operation["params"]["provider"] == provider
        assert operation["params"]["route_url"] == canonical
        assert fixture.store.count(m.operations.name) == before[m.operations.name] + 1
        assert fixture.store.count(m.attempts.name) == before[m.attempts.name]
    # Both rejection and successful normalization leave the saved source raw.
    fixture.store.engine.dispose()
    fixture.store = fixture._new_store()
    with fixture.store.engine.connect() as connection:
        saved_url = connection.execute(select(m.routes.c.url).where(
            m.routes.c.id == route_id,
        )).scalar_one()
    assert saved_url == raw_url
