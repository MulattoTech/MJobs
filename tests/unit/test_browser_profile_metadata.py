"""Bounded profile observations never become browser or authentication authority."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from services.applications import browser_profile_metadata as metadata
from services.applications.browser_profiles import browser_profile_path
from workers.operations import OperationCancelled, OperationControlUnavailable, OperationTimedOut

ORIGIN = "http://127.0.0.1:8123"
PROVIDER = "mock-ats"
FIRST = "2026-11-14T00:00:00.000000Z"
LATER = "2026-11-14T00:01:00.000000Z"


@pytest.fixture
def scope(tmp_path):
    root = tmp_path / "application-one"
    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "review"}):
        profile = browser_profile_path(root, initialize=True)
    return root, profile


def record(scope, stamp=FIRST):
    with patch.object(metadata, "_utc_now", return_value=stamp):
        return metadata.record_browser_profile_use(
            *scope, provider=PROVIDER, site=ORIGIN)


def read(scope):
    return metadata.read_browser_profile_metadata(
        *scope, provider=PROVIDER, site=ORIGIN)


def test_launch_and_observed_authentication_have_distinct_timestamp_semantics(scope):
    root, profile = scope
    selection = (root / "browser-profile-selection.json").read_bytes()
    assert record(scope)
    original = read(scope)
    assert set(original) == {
        "schema_version", "profile_id", "application_id", "profile_name",
        "provider", "site", "status", "last_used", "likely_authenticated", "created_at",
    }
    assert original["application_id"] == root.name
    assert original["profile_name"] == "review"
    assert len(original["profile_id"]) == 64
    assert original["site"] == ORIGIN
    assert original["provider"] == PROVIDER
    assert original["created_at"] == original["last_used"] == FIRST
    assert original["status"] == "AUTHENTICATION_UNKNOWN"
    assert original["likely_authenticated"] is None

    with patch.object(metadata, "_utc_now", side_effect=AssertionError("not another launch")):
        assert metadata.record_browser_profile_auth_required(
            root, profile, provider=PROVIDER, site=ORIGIN)
    waiting = read(scope)
    assert waiting["status"] == "AUTH_REQUIRED"
    assert waiting["likely_authenticated"] is False
    assert waiting["last_used"] == FIRST

    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "different"}):
        assert record(scope, LATER)
    reopened = read(scope)
    assert reopened["created_at"] == FIRST
    assert reopened["last_used"] == LATER
    assert reopened["profile_id"] == original["profile_id"]
    assert reopened["status"] == "AUTHENTICATION_UNKNOWN"
    assert reopened["likely_authenticated"] is None
    assert (root / "browser-profile-selection.json").read_bytes() == selection
    assert not (root / "browser-profile/different").exists()
    path = profile / metadata.METADATA_FILENAME
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_read_only_lookup_and_auth_observation_never_invent_a_launch(scope):
    root, profile = scope
    before = sorted(profile.iterdir())
    assert read(scope) is None
    assert sorted(profile.iterdir()) == before
    assert not metadata.record_browser_profile_auth_required(
        root, profile, provider=PROVIDER, site=ORIGIN)
    assert not (profile / metadata.METADATA_FILENAME).exists()


def test_legal_browser_profile_name_requires_the_explicit_application_root(tmp_path):
    root = tmp_path / "application-nested"
    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "browser-profile"}):
        profile = browser_profile_path(root, initialize=True)
    assert profile == root / "browser-profile/browser-profile"
    assert record((root, profile))
    saved = read((root, profile))
    assert saved["application_id"] == "application-nested"
    assert saved["profile_name"] == "browser-profile"
    before = (profile / metadata.METADATA_FILENAME).read_bytes()
    assert not record((profile.parent, profile), LATER)
    assert not record((None, profile), LATER)
    assert (profile / metadata.METADATA_FILENAME).read_bytes() == before
    assert not (profile.parent / metadata.METADATA_FILENAME).exists()


def test_foreign_profile_or_copied_record_cannot_be_adopted(scope, tmp_path):
    root, profile = scope
    assert record(scope)
    original = (profile / metadata.METADATA_FILENAME).read_bytes()
    other_root = tmp_path / "application-other"
    other = browser_profile_path(other_root, initialize=True)
    assert not record((other_root, profile))
    assert record((other_root, other))
    assert read((other_root, other))["profile_id"] != read(scope)["profile_id"]
    (other / metadata.METADATA_FILENAME).write_bytes(original)
    assert not record((other_root, other), LATER)
    assert (other / metadata.METADATA_FILENAME).read_bytes() == original
    assert (profile / metadata.METADATA_FILENAME).read_bytes() == original


@pytest.mark.parametrize("site", [
    "http://user:PRIVATE_PASSWORD@127.0.0.1:8123",
    "http://127.0.0.1:8123/path?token=PRIVATE_TOKEN",
    "http://127.0.0.1:8123/?token=PRIVATE_TOKEN",
    "http://127.0.0.1:8123/#PRIVATE_FRAGMENT",
    "file:///PRIVATE_PATH", "http://127.0.0.1:bad", "http://127.0.0.1:\n8123",
])
def test_credential_bearing_or_non_origin_inputs_are_refused_without_disclosure(scope, site, caplog):
    assert not metadata.record_browser_profile_use(
        *scope, provider=PROVIDER, site=site)
    assert not (scope[1] / metadata.METADATA_FILENAME).exists()
    assert "PRIVATE" not in caplog.text
    assert site not in caplog.text
    assert str(scope[0]) not in caplog.text


@pytest.mark.parametrize("raw", [
    b"{", b"[]", b"x" * 4097, b"[" * 2000 + b"]" * 2000,
    b'{"schema_version":1,"schema_version":1}',
])
def test_malformed_oversized_or_nested_records_are_preserved(scope, raw, caplog):
    path = scope[1] / metadata.METADATA_FILENAME
    path.write_bytes(raw)
    path.chmod(0o600)
    assert not record(scope)
    assert path.read_bytes() == raw
    with pytest.raises(metadata.BrowserProfileMetadataError, match="^BROWSER_PROFILE_METADATA_UNAVAILABLE$"):
        read(scope)
    assert str(scope[0]) not in caplog.text


@pytest.mark.parametrize("field,value", [
    ("likely_authenticated", True), ("profile_name", "different"),
    ("schema_version", True), ("provider", "foreign"),
    ("site", "http://127.0.0.1:9999"), ("created_at", "PRIVATE_TIMESTAMP"),
])
def test_invalid_or_foreign_fields_are_not_repaired(scope, field, value):
    assert record(scope)
    saved = read(scope)
    saved[field] = value
    path = scope[1] / metadata.METADATA_FILENAME
    raw = json.dumps(saved).encode()
    path.write_bytes(raw)
    assert not record(scope, LATER)
    assert path.read_bytes() == raw


def test_selection_drift_does_not_redirect_existing_profile_metadata(scope):
    root, profile = scope
    assert record(scope)
    before = (profile / metadata.METADATA_FILENAME).read_bytes()
    selection = root / "browser-profile-selection.json"
    selection.write_text('{"schema_version":1,"name":"different"}', encoding="utf-8")
    assert not record(scope, LATER)
    assert (profile / metadata.METADATA_FILENAME).read_bytes() == before
    assert not (root / "browser-profile/different").exists()


@pytest.mark.parametrize("failure_point", ["replace", "fsync"])
def test_atomic_failure_preserves_complete_old_record_and_sanitizes_errors(scope, caplog, failure_point):
    assert record(scope)
    path = scope[1] / metadata.METADATA_FILENAME
    previous = path.read_bytes()
    with patch.object(metadata.os, failure_point,
                      side_effect=PermissionError("PRIVATE_PATH_AND_TOKEN")):
        assert not record(scope, LATER)
    assert path.read_bytes() == previous
    assert not list(scope[1].glob(".mjobs-profile-metadata-*.tmp"))
    assert "PRIVATE_PATH_AND_TOKEN" not in caplog.text
    assert str(scope[0]) not in caplog.text


def test_clock_regression_does_not_rewrite_a_more_recent_observation(scope):
    assert record(scope, LATER)
    path = scope[1] / metadata.METADATA_FILENAME
    before = path.read_bytes()
    assert not record(scope, FIRST)
    assert path.read_bytes() == before


def test_symlink_metadata_is_never_followed(scope, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE_UNRELATED_DATA")
    path = scope[1] / metadata.METADATA_FILENAME
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Creating a file symlink is unavailable on this host")
    assert not record(scope)
    assert path.is_symlink()
    assert outside.read_bytes() == b"PRIVATE_UNRELATED_DATA"


def test_hardlinked_metadata_is_not_read_or_replaced(scope, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE_UNRELATED_DATA")
    path = scope[1] / metadata.METADATA_FILENAME
    try:
        os.link(outside, path)
    except OSError:
        pytest.skip("Creating a hardlink is unavailable on this host")
    assert not record(scope)
    assert path.stat().st_nlink == 2
    assert outside.read_bytes() == b"PRIVATE_UNRELATED_DATA"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO fixture")
def test_special_metadata_file_is_refused_without_blocking(scope):
    os.mkfifo(scope[1] / metadata.METADATA_FILENAME)
    assert not record(scope)


@pytest.mark.parametrize("kind", [OperationCancelled, OperationTimedOut, OperationControlUnavailable])
def test_operation_control_exceptions_propagate_by_identity(scope, kind):
    original = kind() if kind is OperationControlUnavailable else kind("PRIVATE_CONTROL_SENTINEL")
    with patch.object(metadata, "_write", side_effect=original):
        with pytest.raises(kind) as observed:
            record(scope)
    assert observed.value is original


def test_rq_soft_timeout_is_not_downgraded_to_optional_metadata_failure(scope):
    from rq.timeouts import JobTimeoutException
    original = JobTimeoutException("PRIVATE_TIMEOUT_SENTINEL")
    with patch.object(metadata.os, "replace", side_effect=original):
        with pytest.raises(JobTimeoutException) as observed:
            record(scope)
    assert observed.value is original
    assert not list(scope[1].glob(".mjobs-profile-metadata-*.tmp"))


def manual_window(scope, events, *, on_process, navigation_error=None):
    from services.applications import manual_auth
    page = Mock()
    page.is_closed.return_value = True
    if navigation_error is not None:
        page.goto.side_effect = navigation_error
    context = Mock()
    context.pages = [page]
    context.close.side_effect = lambda: events.append("context_close")
    chromium = Mock()
    chromium.launch_persistent_context.return_value = context
    manager = Mock()
    manager.__enter__ = Mock(return_value=SimpleNamespace(chromium=chromium))
    manager.__exit__ = Mock(side_effect=lambda *args: events.append("driver_exit"))
    client = Mock()
    client.origin = ORIGIN
    with patch("playwright.sync_api.sync_playwright", return_value=manager), \
            patch("services.applications.browser_process.capture_browser_identity",
                  return_value={"synthetic_identity": True}):
        status = manual_auth.run_browser(
            scope[1], ORIGIN + "/ats/login/A", client,
            application_root=scope[0], on_process=on_process)
    return status, context, page, client


def test_process_ownership_error_precedes_all_metadata_and_navigation(scope):
    from services.applications import manual_auth
    events = []
    def failed_owner(identity):
        events.append("owner")
        raise RuntimeError("PRIVATE_OWNER_FAILURE")
    with patch.object(metadata, "record_browser_profile_use") as observed:
        status, context, page, client = manual_window(
            scope, events, on_process=failed_owner)
    assert status == manual_auth.OPEN_UNAVAILABLE
    assert events == ["owner", "context_close", "driver_exit"]
    observed.assert_not_called()
    page.goto.assert_not_called()
    context.close.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("kind", [OperationCancelled, OperationTimedOut])
def test_metadata_io_failure_cannot_convert_control_failure_into_success(scope, kind, caplog):
    from services.applications import manual_auth
    events = []
    def owner(identity):
        events.append("owner")
    def failed_metadata(*args, **kwargs):
        events.append("metadata")
        raise PermissionError("PRIVATE_METADATA_FAILURE")
    with patch.object(metadata, "_write", side_effect=failed_metadata):
        status, context, page, client = manual_window(
            scope, events, on_process=owner, navigation_error=kind("PRIVATE_CONTROL_FAILURE"))
    assert status == manual_auth.OPEN_UNAVAILABLE
    assert events == ["owner", "metadata", "context_close", "driver_exit"]
    page.goto.assert_called_once()
    context.close.assert_called_once()
    client.close.assert_called_once()
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize("site,expected", [
    ("http://LOCALHOST:80/", "http://localhost"),
    ("https://EXAMPLE.INVALID:443/", "https://example.invalid"),
    ("http://[::1]:8123/", "http://[::1]:8123"),
])
def test_only_canonical_origin_is_persisted(scope, site, expected):
    with patch.object(metadata, "_utc_now", return_value=FIRST):
        assert metadata.record_browser_profile_use(*scope, provider=PROVIDER, site=site)
    saved = metadata.read_browser_profile_metadata(*scope, provider=PROVIDER, site=expected)
    assert saved["site"] == expected


@pytest.mark.parametrize("missing_root", [True, False])
def test_missing_or_mismatched_root_omits_metadata_without_changing_manual_cleanup(scope, tmp_path, missing_root):
    from services.applications import manual_auth
    if missing_root:
        supplied = None
    else:
        supplied = tmp_path / "foreign-application"
        browser_profile_path(supplied, initialize=True)
    events = []
    status, context, page, client = manual_window(
        (supplied, scope[1]), events, on_process=lambda identity: events.append("owner"))
    assert status == manual_auth.MANUAL_AUTH_WINDOW_CLOSED
    assert events == ["owner", "context_close", "driver_exit"]
    page.goto.assert_called_once()
    context.close.assert_called_once()
    client.close.assert_called_once()
    assert not (scope[1] / metadata.METADATA_FILENAME).exists()
    if supplied is not None:
        assert not (supplied / "browser-profile" / metadata.METADATA_FILENAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX metadata file mode")
def test_permissive_metadata_is_preserved_without_permission_repair(scope):
    assert record(scope)
    path = scope[1] / metadata.METADATA_FILENAME
    previous = path.read_bytes()
    path.chmod(0o644)
    assert not record(scope, LATER)
    assert path.read_bytes() == previous
    assert path.stat().st_mode & 0o777 == 0o644
