"""The recovery coordinator always consults current workspace/application state."""
import pytest

from services.applications.multipage_state import MultipageStateError
from services.applications.workday_recovery import reconcile_workday_checkpoint
from tests.integration.test_fleet_t03_lead_workday_recovery import observation, recovery


def reconcile(store, action, version, **kwargs):
    return reconcile_workday_checkpoint(
        store, "profile", "application", action=action,
        expected_version=version, request_id=f"direct-{action}-{version}",
        current_observation=observation("My Information"),
        operation_mode="FIXTURE", **kwargs,
    )


def test_authoritative_version_and_workspace_prevent_stale_resume(recovery):
    store, _, _ = recovery
    created = reconcile(store, "INITIALIZE", 0)
    assert created["decision"] == "INITIALIZED"
    resumed = reconcile(store, "RESUME", 1)
    assert resumed["decision"] == "RESUME_FROM_CONFIRMED_STEP"
    assert resumed["state"] == created["state"]
    assert resumed["execute"] is False
    with pytest.raises(MultipageStateError, match="reload") as error:
        reconcile(store, "RESUME", 0)
    assert error.value.code == "MULTIPAGE_VERSION_CONFLICT"
    with pytest.raises(MultipageStateError) as error:
        reconcile_workday_checkpoint(
            store, "profile", "application", action="RESUME", request_id="wrong-mode",
            expected_version=1, current_observation=observation("My Information"),
            operation_mode="PERSONAL",
        )
    assert error.value.code == "WORKSPACE_MODE_MISMATCH"


@pytest.mark.parametrize("action,version,request_id,code", [
    ("FINAL_SUBMIT", 0, "request", "INVALID_ACTION"),
    ("INITIALIZE", True, "request", "INVALID_VERSION"),
    ("INITIALIZE", 0, "request with spaces", "INVALID_REQUEST_ID"),
])
def test_invalid_direct_requests_never_persist(recovery, action, version, request_id, code):
    store, _, _ = recovery
    with pytest.raises(MultipageStateError) as error:
        reconcile_workday_checkpoint(
            store, "profile", "application", action=action, request_id=request_id,
            expected_version=version, current_observation=observation("My Information"),
            operation_mode="FIXTURE",
        )
    assert error.value.code == code
    assert store.count("audit_events") == 0
