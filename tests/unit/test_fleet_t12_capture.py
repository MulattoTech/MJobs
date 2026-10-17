"""Bounded synthetic checks for the shared screenshot entry point."""
from concurrent.futures import CancelledError
from threading import Event
from unittest.mock import Mock

import pytest

from services.applications.browser_capture import capture_redacted_screenshot


def page_fixture():
    page = Mock()
    page.evaluate.side_effect = AssertionError("Unbounded renderer evaluation")
    return page


def test_capture_uses_one_finite_operation_without_page_evaluation():
    page = page_fixture()
    capture_redacted_screenshot(page, "synthetic.png")
    page.evaluate.assert_not_called()
    assert 0 < page.screenshot.call_args.kwargs["timeout"] <= 5000
    assert page.screenshot.call_count == 1


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout_fails_before_touching_browser(timeout):
    page = page_fixture()
    with pytest.raises(ValueError):
        capture_redacted_screenshot(page, "synthetic.png", timeout_ms=timeout)
    assert page.mock_calls == []


def test_primary_screenshot_failure_is_preserved_without_cleanup_evaluation():
    page = Mock()
    failure = RuntimeError("synthetic capture failure")
    page.screenshot.side_effect = failure
    page.evaluate.side_effect = [None, RuntimeError("synthetic cleanup failure")]
    with pytest.raises(RuntimeError) as caught:
        capture_redacted_screenshot(page, "synthetic.png", timeout_ms=100)
    assert caught.value is failure
    page.evaluate.assert_not_called()
    assert page.screenshot.call_count == 1


def test_precancelled_capture_does_not_touch_page_or_output(tmp_path):
    page = page_fixture()
    cancel = Event()
    cancel.set()
    path = tmp_path / "cancelled.png"
    with pytest.raises(CancelledError):
        capture_redacted_screenshot(page, path, cancel_event=cancel)
    assert page.mock_calls == []
    assert not path.exists()


def test_expired_deadline_does_not_touch_browser(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 50)
    page = page_fixture()
    with pytest.raises(TimeoutError):
        capture_redacted_screenshot(page, "synthetic.png", deadline=49)
    assert page.mock_calls == []


def test_shared_deadline_caps_the_screenshot_budget(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 50)
    page = page_fixture()
    capture_redacted_screenshot(page, "synthetic.png", timeout_ms=2000, deadline=50.25)
    assert 0 < page.screenshot.call_args.kwargs["timeout"] <= 250


def test_caller_timeout_is_finite_and_preserved():
    page = page_fixture()
    capture_redacted_screenshot(page, "synthetic.png", timeout_ms=150, full_page=False)
    assert page.screenshot.call_args.kwargs["timeout"] <= 150
    assert page.screenshot.call_args.kwargs["full_page"] is False


@pytest.mark.parametrize("deadline", [float("inf"), float("nan"), True, "tomorrow"])
def test_invalid_deadline_fails_before_touching_browser(deadline):
    page = page_fixture()
    with pytest.raises(ValueError):
        capture_redacted_screenshot(page, "synthetic.png", deadline=deadline)
    assert page.mock_calls == []


def test_large_timeout_is_capped():
    page = page_fixture()
    capture_redacted_screenshot(page, "synthetic.png", timeout_ms=900000)
    assert 0 < page.screenshot.call_args.kwargs["timeout"] <= 30000


def test_cancellation_during_mask_setup_prevents_capture():
    page = page_fixture()
    cancel = Event()
    page.locator.side_effect = lambda selector: cancel.set()
    with pytest.raises(CancelledError):
        capture_redacted_screenshot(page, "synthetic.png", cancel_event=cancel)
    page.screenshot.assert_not_called()


def test_deadline_expiry_during_mask_setup_prevents_capture(monkeypatch):
    clock = iter([50, 52])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    page = page_fixture()
    with pytest.raises(TimeoutError):
        capture_redacted_screenshot(page, "synthetic.png", deadline=51)
    page.screenshot.assert_not_called()


def test_failure_package_readback_and_next_capture_are_isolated(tmp_path):
    import json
    from pathlib import Path
    from services.applications.debug_capture import capture_browser_failure

    page = Mock()
    page.url = "https://fixture.invalid/application?token=SYNTHETIC_PRIVATE"
    page.evaluate.return_value = {"element_counts": {"input": 1}, "role_counts": {}}
    page.screenshot.side_effect = RuntimeError("SYNTHETIC_PRIVATE")

    first = capture_browser_failure(
        page, tmp_path, attempt_id="a" * 32, step="DETECT_FORM",
        configured_url=page.url, error=RuntimeError("SYNTHETIC_PRIVATE"),
    )
    first_metadata = json.loads((first / "metadata.json").read_text())
    assert first_metadata["screenshot"]["status"] == "FAILED"
    assert not (first / "screenshot.png").exists()
    assert "SYNTHETIC_PRIVATE" not in (first / "metadata.json").read_text()

    def write_capture(**kwargs):
        Path(kwargs["path"]).write_bytes(b"synthetic-redacted-layout")

    page.screenshot.side_effect = write_capture
    second = capture_browser_failure(
        page, tmp_path, attempt_id="b" * 32, step="DETECT_FORM",
        configured_url=page.url, error=RuntimeError("synthetic next failure"),
    )
    second_metadata = json.loads((second / "metadata.json").read_text())
    assert second_metadata["screenshot"]["status"] == "CAPTURED"
    assert (second / "screenshot.png").read_bytes() == b"synthetic-redacted-layout"
    assert json.loads((first / "metadata.json").read_text()) == first_metadata
