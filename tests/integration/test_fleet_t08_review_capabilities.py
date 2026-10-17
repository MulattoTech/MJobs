"""Value-free, current application capability evidence for the review UI."""
from unittest.mock import patch

import pytest
from sqlalchemy import update

from packages.persistence import models as m
from tests.integration.test_fleet_t08_provider_identity import fixture_for, set_route


URL = "https://fixture.breezy.hr/p/012345abcdef-engineer"


def expected(fixture, supported):
    return {
        "schema_version": 1,
        "profile_id": "fixture",
        "application_id": fixture.application_id,
        "provider": "breezy" if supported else None,
        "review_supported": supported,
        "read_only": True,
        "submission_supported": False,
    }


def test_capability_is_current_registry_route_evidence_without_side_effects():
    with fixture_for("breezy") as fixture:
        set_route(fixture, URL, "fixture", "012345abcdef")
        path = f"/profiles/fixture/applications/{fixture.application_id}/form-review-capabilities"
        before = tuple(fixture.store.count(table) for table in (
            "worker_operations", "audit_events", "application_attempts",
        ))
        with patch("apps.api.application_form_review.enqueue_operation") as enqueue:
            response = fixture.client.get(path)
            assert response.status_code == 200, response.text
            assert response.json() == expected(fixture, True)
            with patch("apps.api.application_form_review.providers", return_value=()) as registry:
                denied = fixture.client.get(path)
                registry.assert_called_once()
            assert denied.json() == expected(fixture, False)
            set_route(fixture, " " + URL, "fixture", "012345abcdef")
            assert fixture.client.get(path).json() == expected(fixture, False)
        enqueue.assert_not_called()
        assert tuple(fixture.store.count(table) for table in (
            "worker_operations", "audit_events", "application_attempts",
        )) == before
        assert fixture.client.get(path.replace("/fixture/", "/other-profile/")).status_code == 404
        assert fixture.client.get(path.replace(fixture.application_id, "missing-application")).status_code == 404


@pytest.mark.parametrize("unsupported", ["provider", "mock", "target"])
def test_unsupported_or_non_real_application_has_no_review_capability(unsupported):
    with fixture_for("breezy") as fixture:
        route_id = set_route(fixture, URL, "fixture", "012345abcdef")
        with fixture.store.engine.begin() as connection:
            if unsupported == "provider":
                connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                    provider="unknown-private-provider",
                ))
            else:
                connection.execute(update(m.applications).where(
                    m.applications.c.id == fixture.application_id,
                ).values(target="MOCK" if unsupported == "mock" else "UNKNOWN"))
        response = fixture.client.get(
            f"/profiles/fixture/applications/{fixture.application_id}/form-review-capabilities",
        )
        assert response.status_code == 200, response.text
        assert response.json() == expected(fixture, False)
