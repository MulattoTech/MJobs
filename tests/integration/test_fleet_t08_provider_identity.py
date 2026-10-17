"""Consumed provider identity controls through public durable form review."""
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from services.applications.provider_registry import get_provider
from tests.integration import test_application_form_review_flow as flow_fixture


PROVIDERS = ("smartrecruiters", "workable", "teamtailor", "bamboohr")


def route(provider, tenant="kteam"):
    if provider == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{tenant}/123456789012-special", "123456789012"
    if provider == "workable":
        return f"https://apply.workable.com/{tenant}/j/abc12s", "abc12s"
    if provider == "teamtailor":
        return f"https://{tenant}.teamtailor.com/jobs/123-special", "123-special"
    return f"https://{tenant}.bamboohr.com/careers/123", "123"


@contextmanager
def fixture_for(provider):
    fixture = flow_fixture.ApplicationFormReviewFlowTests()
    fixture.setUp()
    try:
        fixture.provider = provider
        yield fixture
    finally:
        fixture.tearDown()


def set_route(fixture, url, tenant, requisition):
    route_id = fixture.store.application(fixture.application_id)["route_id"]
    with fixture.store.engine.begin() as connection:
        connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
            provider=fixture.provider, tenant=tenant, requisition=requisition, url=url,
        ))
    return route_id


def stored_route(fixture, route_id):
    with fixture.store.engine.connect() as connection:
        return dict(connection.execute(select(m.routes).where(
            m.routes.c.id == route_id,
        )).mappings().one())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_ascii_identity_reaches_selected_adapter_worker_and_durable_readback(provider):
    with fixture_for(provider) as fixture:
        canonical, requisition = route(provider)
        raw = canonical.replace("kteam", "KTEAM")
        route_id = set_route(fixture, raw, "KTEAM", requisition.upper())
        original = stored_route(fixture, route_id)
        fixture.route_url = canonical
        adapter_class = type(get_provider(provider))
        normalize = adapter_class.normalize_route
        with patch.object(adapter_class, "normalize_route", autospec=True, side_effect=normalize) as selected:
            path = fixture._complete_review("t08-ascii-identity", "email")
            exact = fixture.client.get(path + "/t08-ascii-identity")
            latest = fixture.client.get(path + "/latest")
        assert selected.call_count >= 3  # admission, worker and latest use the real adapter
        assert exact.status_code == latest.status_code == 200
        assert exact.json() == latest.json()
        assert exact.json()["state"] == "COMPLETED"
        assert exact.json()["analysis"]["status"] == "REVIEW_REQUIRED"
        assert exact.json()["analysis"]["reason"] in {
            "DOM_INSPECTION_REQUIRED", "FORM_SEMANTICS_UNSUPPORTED",
        }
        assert fixture.store.count("application_attempts") == 0
        assert stored_route(fixture, route_id) == original
        with fixture.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                m.operations.c.entity_id == fixture.application_id,
            )).mappings().one()
            receipts = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == fixture.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).scalars())
        assert operation["params"]["provider"] == provider
        assert operation["params"]["route_url"] == canonical
        assert len(receipts) == 1
        assert receipts[0]["analysis"] == exact.json()["analysis"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_raw_unicode_and_space_are_rejected_before_enqueue_without_mutation(provider):
    with fixture_for(provider) as fixture:
        cases = []
        for ascii_tenant, raw_tenant in (("steam", "\u017fteam"), ("kteam", "\u212ateam")):
            url, requisition = route(provider, ascii_tenant)
            cases.extend((
                (url.replace(ascii_tenant, raw_tenant), ascii_tenant, requisition),
                (url, raw_tenant, requisition),
            ))
        url, requisition = route(provider)
        cases.extend(((" " + url, "kteam", requisition),
                      (url.replace("kteam", "\tkteam"), "kteam", requisition)))
        if provider == "smartrecruiters":
            cases.append((url.replace("special", "\u017fpecial"), "kteam", requisition))
            cases.append((url.replace("smartrecruiters", "smartrecruiter\u017f"), "kteam", requisition))
        if provider in {"workable", "teamtailor"}:
            cases.extend(((url, "kteam", requisition.replace("s", "\u017f")),
                          (url.replace(requisition, requisition.replace("s", "\u017f")), "kteam", requisition)))
        adapter_class = type(get_provider(provider))
        normalize = adapter_class.normalize_route
        path = f"/profiles/fixture/applications/{fixture.application_id}/form-reviews"
        for number, (raw_url, tenant, requisition) in enumerate(cases):
            route_id = set_route(fixture, raw_url, tenant, requisition)
            original = stored_route(fixture, route_id)
            before = tuple(fixture.store.count(table) for table in (
                "worker_operations", "audit_events", "application_attempts",
            ))
            with patch.object(adapter_class, "normalize_route", autospec=True, side_effect=normalize) as selected:
                response = fixture.client.post(path, json={"request_id": f"t08-invalid-{number}"})
            assert selected.called, "a hardcoded API rejection is not adapter verification"
            assert response.status_code == 409, (provider, number, response.text)
            assert response.json()["detail"]["code"] == "FORM_REVIEW_ROUTE_UNSUPPORTED"
            assert tuple(fixture.store.count(table) for table in (
                "worker_operations", "audit_events", "application_attempts",
            )) == before
            assert stored_route(fixture, route_id) == original
