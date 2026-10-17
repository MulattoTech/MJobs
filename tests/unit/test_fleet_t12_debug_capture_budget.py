"""Synthetic budget, cancellation, privacy and recovery tests for T12 capture."""
import json
import math
from pathlib import Path

import pytest

from services.applications.debug_capture import (
    SCREENSHOT_TIMEOUT_MS,
    capture_browser_failure,
)
from workers.operations import OperationCancelled


class _Check:
    def __init__(self, remaining, *, cancel_on=None, on_check=None):
        self._remaining = remaining
        self._cancel_on = cancel_on
        self._on_check = on_check
        self.calls = 0
        self.remaining_calls = 0

    def __call__(self):
        self.calls += 1
        if self._on_check is not None:
            self._on_check()
        if self.calls == self._cancel_on:
            raise OperationCancelled("synthetic cancellation")

    def remaining_seconds(self):
        self.remaining_calls += 1
        return self._remaining


class _Page:
    url = "https://fixture.invalid/apply?token=SYNTHETIC_PRIVATE"

    def __init__(self, *, screenshot_error=None, clock=None):
        self.screenshot_error = screenshot_error
        self.clock = clock
        self.locators = []
        self.screenshots = []
        self.evaluate_calls = 0

    def evaluate(self, *_args, **_kwargs):
        self.evaluate_calls += 1
        raise AssertionError("capture must not evaluate page JavaScript")

    def locator(self, selector):
        self.locators.append(selector)
        if self.clock is not None:
            self.clock[0] += .08
        return selector

    def screenshot(self, **kwargs):
        self.screenshots.append(kwargs)
        if self.screenshot_error is not None:
            raise self.screenshot_error
        Path(kwargs["path"]).write_bytes(b"synthetic-redacted-screenshot")


def _capture(page, root, attempt_id, error, check):
    return capture_browser_failure(
        page, root, attempt_id=attempt_id, step="DETECT_FORM",
        configured_url="https://fixture.invalid/apply", error=error, check=check,
    )


def test_precancelled_capture_does_not_touch_page_or_create_output(tmp_path):
    root = tmp_path / "cancelled"
    page = _Page()
    check = _Check(1, cancel_on=1)

    result = _capture(page, root, "a" * 32, RuntimeError("primary failure"), check)

    assert result is None
    assert check.calls == 1
    assert page.evaluate_calls == 0
    assert page.locators == []
    assert page.screenshots == []
    assert not root.exists()


@pytest.mark.parametrize("remaining", [
    0, -1, .0001, True, None, "1", math.inf, math.nan,
])
def test_expired_or_invalid_budget_does_not_touch_page_or_create_output(tmp_path, remaining):
    root = tmp_path / "invalid-budget"
    page = _Page()
    check = _Check(remaining)

    result = _capture(page, root, "b" * 32, RuntimeError("primary failure"), check)

    assert result is None
    assert check.remaining_calls == 1
    assert page.evaluate_calls == 0
    assert page.locators == []
    assert page.screenshots == []
    assert not root.exists()


def test_budget_is_fixed_once_and_screenshot_timeout_shrinks_across_phases(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    page = _Page(clock=clock)
    check = _Check(1.7, on_check=lambda: clock.__setitem__(0, clock[0] + .02))

    capture = _capture(page, tmp_path / "shared-budget", "c" * 32,
        RuntimeError("primary failure"), check)

    assert capture is not None
    assert check.remaining_calls == 1
    assert check.calls >= 2
    assert page.evaluate_calls == 0
    assert len(page.screenshots) == 1
    assert 0 < page.screenshots[0]["timeout"] < 1300
    assert page.screenshots[0]["timeout"] <= SCREENSHOT_TIMEOUT_MS


def test_caller_budget_is_capped_at_two_second_screenshot_limit(tmp_path):
    page = _Page()
    check = _Check(30)

    capture = _capture(page, tmp_path / "capped-budget", "1" * 32,
        RuntimeError("primary failure"), check)

    assert capture is not None
    assert check.remaining_calls == 1
    assert 0 < page.screenshots[0]["timeout"] <= SCREENSHOT_TIMEOUT_MS


def test_check_without_remaining_seconds_uses_fixed_screenshot_budget(tmp_path):
    page = _Page()
    calls = []

    def check():
        calls.append("checked")

    capture = capture_browser_failure(
        page, tmp_path / "optional-budget", attempt_id="2" * 32,
        step="DETECT_FORM", configured_url="https://fixture.invalid/apply",
        error=RuntimeError("primary failure"), check=check,
    )

    assert capture is not None
    assert len(calls) >= 2
    assert 0 < page.screenshots[0]["timeout"] <= SCREENSHOT_TIMEOUT_MS


def test_midphase_cancellation_returns_none_without_replacing_primary_error(tmp_path):
    root = tmp_path / "midphase-cancel"
    page = _Page()
    check = _Check(1.5, cancel_on=2)

    result = _capture(page, root, "d" * 32, ValueError("original browser error"), check)

    assert result is None
    assert check.calls == 2
    assert page.evaluate_calls == 0
    assert page.screenshots == []
    assert not (root / ("d" * 32)).exists()


def test_initial_one_shot_cancellation_is_terminal(tmp_path):
    root = tmp_path / "initial-one-shot-cancel"
    page = _Page()

    class CancelOnce:
        calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 1:
                raise OperationCancelled("initial cancellation")

        def remaining_seconds(self):
            return 1.5

    check = CancelOnce()
    result = _capture(page, root, "7" * 32, RuntimeError("primary failure"), check)

    assert result is None
    assert check.calls == 1
    assert page.locators == []
    assert page.screenshots == []
    assert not root.exists()


def test_post_screenshot_one_shot_cancellation_is_not_screenshot_failure(
        tmp_path, monkeypatch):
    root = tmp_path / "post-screenshot-one-shot-cancel"
    page = _Page()
    primary = ValueError("original primary browser error")

    class CancelAfterScreenshot:
        def __init__(self):
            self.calls = 0
            self.cancel_next = False
            self.cancel_at = None

        def __call__(self):
            self.calls += 1
            if self.cancel_next:
                self.cancel_next = False
                self.cancel_at = self.calls
                raise OperationCancelled("post-screenshot cancellation")

        def remaining_seconds(self):
            return 1.5

    check = CancelAfterScreenshot()
    written = []

    def write_masked_screenshot(_page, path, **_kwargs):
        path.write_bytes(b"synthetic-masked-png")
        written.append(path)
        check.cancel_next = True

    monkeypatch.setattr(
        "services.applications.debug_capture.capture_redacted_screenshot",
        write_masked_screenshot,
    )
    original_args = primary.args

    result = _capture(page, root, "8" * 32, primary, check)

    assert result is None
    assert len(written) == 1
    assert check.cancel_at is not None
    assert check.calls == check.cancel_at  # No continuation after control signal.
    assert primary.args == original_args
    assert primary.args == ("original primary browser error",)
    assert not (root / ("8" * 32) / "metadata.json").exists()
    assert page.evaluate_calls == 0


def test_omitted_dom_primary_error_metadata_and_healthy_next_capture_readback(tmp_path):
    root = tmp_path / "readback"
    primary = ValueError("ORIGINAL_BROWSER_FAILURE")
    first_page = _Page(screenshot_error=TimeoutError("diagnostic timeout"))
    first = _capture(first_page, root, "e" * 32, primary, _Check(1.5))

    assert first is not None
    first_metadata_bytes = (first / "metadata.json").read_bytes()
    metadata = json.loads(first_metadata_bytes)
    dom = json.loads((first / "dom.json").read_bytes())
    assert metadata["error"]["type"] == "ValueError"
    assert metadata["error"]["message_length"] == len(str(primary).encode())
    assert "ORIGINAL_BROWSER_FAILURE" not in first_metadata_bytes.decode()
    assert metadata["screenshot"]["status"] == "FAILED"
    assert not (first / "screenshot.png").exists()
    assert dom == {
        "status": "OMITTED_UNBOUNDED_EVALUATION",
        "element_counts": {},
        "role_counts": {},
        "text_omitted": True,
        "attributes_omitted": True,
        "control_values_omitted": True,
    }
    assert first_page.evaluate_calls == 0

    next_page = _Page()
    second = _capture(next_page, root, "f" * 32,
        RuntimeError("synthetic next failure"), _Check(1.5))

    assert second is not None
    assert json.loads((second / "metadata.json").read_bytes())["screenshot"]["status"] == "CAPTURED"
    assert (second / "screenshot.png").read_bytes() == b"synthetic-redacted-screenshot"
    assert (first / "metadata.json").read_bytes() == first_metadata_bytes
    assert next_page.evaluate_calls == 0
