"""User declaration metadata is display-only and stays current on readback."""
import pytest

from services.applications.manual_auth import revoke_user_auth_completion
from tests.integration import test_fleet_t01_manual_auth_completion as auth_fixture


@pytest.fixture
def review():
    fixture = auth_fixture.ManualAuthCompletionTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def test_exact_and_latest_display_declaration_without_changing_review_state(review):
    declared = review.record()
    path = f"/profiles/fixture/applications/{review.application_id}/form-reviews"
    expected = {key: declared[key] for key in (
        "status", "review_operation_id", "declared_at", "expires_at",
        "authenticated_session_available",
    )}
    for suffix in ("/fleet-t01-service", "/latest"):
        response = review.client.get(path + suffix)
        assert response.status_code == 200
        assert response.json().get("user_auth_declaration") == expected
        assert response.json()["state"] == "QUEUED"
        assert response.json()["analysis"] is None
    assert expected["status"] == "USER_DECLARED"
    assert expected["authenticated_session_available"] is False
    assert review.store.count("application_attempts") == 0


def test_revoked_declaration_disappears_from_exact_readback(review):
    review.record()
    revoke_user_auth_completion(review.store, "fixture", review.application_id, review.review_id)
    path = f"/profiles/fixture/applications/{review.application_id}/form-reviews/fleet-t01-service"
    response = review.client.get(path)
    assert response.status_code == 200
    assert response.json().get("user_auth_declaration") is None
    assert response.json()["state"] == "QUEUED"
