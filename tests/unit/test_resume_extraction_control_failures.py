"""Control failures are fatal across optional native/OCR fallback boundaries."""
from contextlib import ExitStack
import subprocess
import sys
from unittest.mock import patch

import pytest

from services.career_profile import extraction, ocr
from workers.operations import (
    OperationCancelled, OperationControlUnavailable, OperationTimedOut,
)


class OneShotControl:
    """Model a failed durable check that would succeed if incorrectly retried."""
    def __init__(self, active, failure):
        self.active = active
        self.failure = failure
        self.failures = 0

    def is_set(self):
        if self.active() and not self.failures:
            self.failures += 1
            raise self.failure
        return False


@pytest.mark.parametrize("stage", ["pdf", "docx", "ocr"])
@pytest.mark.parametrize("error_type", [
    OperationCancelled, OperationTimedOut, OperationControlUnavailable,
])
def test_control_failure_preserves_identity_and_reaps_owned_child(stage, error_type):
    children = []
    failure = error_type()
    control = OneShotControl(lambda: bool(children), failure)
    real_popen = subprocess.Popen

    def launch(_command, **kwargs):
        child = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(subprocess, "Popen", side_effect=launch))
            if stage == "ocr":
                stack.enter_context(patch.object(
                    extraction, "_run_native_parser",
                    return_value=(["Native text", ""], [], [2])))
                stack.enter_context(patch.object(ocr, "RESOURCE_LIMITS_AVAILABLE", True))
                stack.enter_context(patch.object(
                    ocr.shutil, "which", side_effect=lambda name: "/synthetic/" + name))
            with pytest.raises(error_type) as raised:
                extraction.extract_resume(
                    b"synthetic document", "pdf" if stage == "ocr" else stage,
                    "synthetic-control", cancellation_event=control)
        assert raised.value is failure
        assert control.failures == 1
        assert len(children) == 1
        assert children[0].poll() is not None, "owned parser/OCR child survived"
        if stage == "ocr":
            assert ocr._SLOT.acquire(blocking=False), "OCR slot leaked after stop"
            ocr._SLOT.release()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_callback_failure_is_fatal_without_importing_worker_types(error_type):
    failure = error_type("SYNTHETIC_PRIVATE_CONTROL_DETAIL")
    entered_ocr = []
    control = OneShotControl(lambda: bool(entered_ocr), failure)
    real_ocr = ocr.ocr_pdf_pages

    def enter_ocr(*args, **kwargs):
        entered_ocr.append(True)
        return real_ocr(*args, **kwargs)

    with patch.object(extraction, "_run_native_parser",
                      return_value=(["Native text"], [], [1])), \
            patch.object(ocr, "ocr_pdf_pages", side_effect=enter_ocr):
        with pytest.raises(error_type) as raised:
            extraction.extract_resume(
                b"synthetic document", "pdf", "synthetic-control",
                cancellation_event=control)
    assert raised.value is failure
    assert control.failures == 1


def test_ordinary_ocr_failure_retains_native_text_and_omits_private_error():
    with patch.object(extraction, "_run_native_parser",
                      return_value=(["Native text"], [], [1])), \
            patch.object(ocr, "ocr_pdf_pages",
                         side_effect=RuntimeError("SYNTHETIC_PRIVATE_ENGINE_DETAIL")):
        result = extraction.extract_resume_text(b"synthetic document", "pdf")
    assert result["status"] == "READY_FOR_REVIEW"
    assert result["text"] == "Native text"
    assert result["used_ocr"] is False
    assert result["warnings"] == [
        "Local OCR failed. Native text remains available for review."]


def test_boolean_cancellation_during_ocr_remains_extraction_cancellation():
    entered_ocr = []
    real_ocr = ocr.ocr_pdf_pages

    class Cancellation:
        def is_set(self):
            return bool(entered_ocr)

    def enter_ocr(*args, **kwargs):
        entered_ocr.append(True)
        return real_ocr(*args, **kwargs)

    with patch.object(extraction, "_run_native_parser",
                      return_value=(["Native text"], [], [1])), \
            patch.object(ocr, "ocr_pdf_pages", side_effect=enter_ocr):
        with pytest.raises(extraction.ResumeExtractionCancelled):
            extraction.extract_resume_text(
                b"synthetic document", "pdf", cancellation_event=Cancellation())
