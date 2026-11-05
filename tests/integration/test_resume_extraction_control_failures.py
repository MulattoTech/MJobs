"""Stopped resume extraction must not publish suggestions after a recovered check."""
from contextlib import ExitStack
import logging
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from apps.api.main import create_app
from fixtures.resume_scans import scanned_resume
from packages.persistence.store import Store
from services.career_profile import extraction, ocr
from services.career_profile.durable_extraction import queue_extraction
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE
from workers import tasks
from workers.operations import (
    OperationCancelled, OperationControlUnavailable, OperationTimedOut,
)


@pytest.mark.parametrize("stage,error_type,expected_state,expected_error", [
    ("ocr", OperationCancelled, "CANCELLED", "CANCEL_REQUESTED"),
    ("ocr", OperationTimedOut, "DEAD_LETTERED", "OPERATION_TIMEOUT"),
    ("ocr", OperationControlUnavailable, "BLOCKED", "OPERATION_CONTROL_UNAVAILABLE"),
    ("pdf", OperationTimedOut, "DEAD_LETTERED", "OPERATION_TIMEOUT"),
])
def test_failed_control_is_durable_without_results_and_new_intent_can_recover(
        tmp_path, monkeypatch, caplog, stage, error_type, expected_state, expected_error):
    database_url = f"sqlite:///{(tmp_path / 'control.db').as_posix()}"
    store = Store(database_url)
    children = []
    real_popen = subprocess.Popen
    real_check = tasks.check_cancelled
    injected = []
    failure = (error_type() if error_type is OperationControlUnavailable
               else error_type("SYNTHETIC_PRIVATE_CONTROL_DETAIL"))

    def launch(_command, **kwargs):
        child = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    def fail_one_check(*args, **kwargs):
        real_check(*args, **kwargs)
        if children and not injected:
            injected.append(True)
            raise failure

    try:
        store.migrate()
        store.bind_workspace_mode("PERSONAL")
        profile_id = "control-" + uuid4().hex[:12]
        store.save_profile(dict(PROFILE, id=profile_id, email="control@example.invalid"))
        before = store.profile(profile_id)
        data = scanned_resume(mixed=True)
        resume = save_resume(
            store, tmp_path, profile_id, "synthetic.pdf", "application/pdf", data)
        intent = "control-intent-" + uuid4().hex
        operation_id = queue_extraction(store, profile_id, resume, intent)["operation_id"]
        monkeypatch.setenv("MJOBS_DATABASE_URL", database_url)
        monkeypatch.setenv("MJOBS_OUTPUT_DIR", str(tmp_path))
        monkeypatch.setenv("MJOBS_OPERATION_MODE", "PERSONAL")
        with ExitStack() as stack:
            stack.enter_context(patch.object(tasks, "check_cancelled", side_effect=fail_one_check))
            stack.enter_context(patch.object(subprocess, "Popen", side_effect=launch))
            if stage == "ocr":
                stack.enter_context(patch.object(
                    extraction, "_run_native_parser",
                    return_value=(["Native text", ""], [], [2])))
                stack.enter_context(patch.object(ocr, "RESOURCE_LIMITS_AVAILABLE", True))
                stack.enter_context(patch.object(
                    ocr.shutil, "which", side_effect=lambda name: "/synthetic/" + name))
            with caplog.at_level(logging.INFO, logger="mjobs.operations"):
                result = tasks.execute_operation(operation_id)
        assert injected == [True]
        assert len(children) == 1
        assert children[0].poll() is not None, "owned child was not reaped"
        reopened = Store(database_url)
        try:
            with TestClient(create_app(
                    reopened, "http://127.0.0.1:1", tmp_path,
                    operation_mode="PERSONAL")) as client:
                endpoint = f"/profiles/{profile_id}/resumes/{resume['id']}/extract/queued"
                response = client.get(f"{endpoint}/{operation_id}")
                assert response.status_code == 200, response.text
                readback = response.json()
                assert result["status"] == expected_state
                assert readback["state"] == expected_state
                assert readback["error"] == expected_error
                assert "result" not in readback
                assert reopened.count("resume_extraction_results") == 0
                assert reopened.profile(profile_id) == before
                assert (tmp_path / "uploads" / f"{resume['id']}.pdf").read_bytes() == data
                replay = queue_extraction(reopened, profile_id, resume, intent)
                assert replay["operation_id"] == operation_id
                assert replay["state"] == expected_state

                # Recovery is a new explicit intent; the failed intent is immutable.
                recovered_id = queue_extraction(
                    reopened, profile_id, resume, "recovery-" + uuid4().hex)["operation_id"]
                with patch.object(extraction, "_run_native_parser",
                                  return_value=(["Skills\nPython"], [], [])):
                    assert tasks.execute_operation(recovered_id)["status"] == "COMPLETED"
                recovered = client.get(f"{endpoint}/{recovered_id}").json()
                assert recovered["state"] == "COMPLETED"
                assert recovered["result"]["facts"]
                assert reopened.count("resume_extraction_results") == 1
                assert client.get(f"{endpoint}/{operation_id}").json()["state"] == expected_state
                assert reopened.profile(profile_id) == before
        finally:
            reopened.engine.dispose()
        assert "SYNTHETIC_PRIVATE_CONTROL_DETAIL" not in caplog.text
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        store.engine.dispose()
