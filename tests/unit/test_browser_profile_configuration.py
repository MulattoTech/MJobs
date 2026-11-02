"""Configuration cannot switch a retained session or select an arbitrary path."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from services.applications.browser_profiles import browser_profile_path, configured_browser_profile


@pytest.mark.parametrize("value", ["", "../outside", "/tmp", "C:\\session", "Two", " a",
                                  "a.b", "a" * 33, "con", "nul", "com1", "lpt9"])
def test_invalid_selection_does_not_create_or_pin_a_profile(tmp_path, value):
    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": value}):
        with pytest.raises(ValueError, match="^MJOBS_BROWSER_PROFILE_INVALID$"):
            browser_profile_path(tmp_path / "app", initialize=True)
    assert not (tmp_path / "app").exists()


def test_new_profile_selection_is_isolated_and_pinned_across_environment_changes(tmp_path):
    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "review"}):
        first = browser_profile_path(tmp_path / "app-one", initialize=True)
        second = browser_profile_path(tmp_path / "app-two", initialize=True)
    assert first == tmp_path / "app-one/browser-profile/review"
    assert first != second and first.is_dir() and second.is_dir()
    (first / "Cookies").write_bytes(b"synthetic-cookie")
    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "another"}):
        assert browser_profile_path(tmp_path / "app-one", initialize=True) == first
        assert browser_profile_path(tmp_path / "app-one") == first
    assert (first / "Cookies").read_bytes() == b"synthetic-cookie"
    assert json.loads((tmp_path / "app-one/browser-profile-selection.json").read_text()) == {
        "schema_version": 1, "name": "review"}


def test_legacy_session_is_preserved_even_when_default_changes(tmp_path):
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"legacy-synthetic-cookie")
    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "new-profile"}):
        assert browser_profile_path(tmp_path, initialize=True) == profile
    assert (profile / "Cookies").read_bytes() == b"legacy-synthetic-cookie"
    assert json.loads((tmp_path / "browser-profile-selection.json").read_text())["name"] == "default"


@pytest.mark.parametrize("raw", [b'{', b'x' * 257,
    b'{"schema_version":true,"name":"review"}',
    b'{"schema_version":1,"name":"../elsewhere"}',
    b'{"schema_version":1,"name":"review","name":"other"}',
    b'{"schema_version":1,"name":"review","cookies":"secret"}'])
def test_invalid_manifest_is_not_overwritten_or_followed(tmp_path, raw):
    selection = tmp_path / "browser-profile-selection.json"
    selection.write_bytes(raw)
    with pytest.raises(ValueError, match="^BROWSER_PROFILE_SELECTION_INVALID$"):
        browser_profile_path(tmp_path, initialize=True)
    assert selection.read_bytes() == raw
    assert not (tmp_path / "browser-profile").exists()


def test_symlinked_profile_cannot_reuse_another_session(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    app = tmp_path / "app"
    app.mkdir()
    try:
        (app / "browser-profile").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable on this test host")
    with pytest.raises(ValueError, match="^BROWSER_PROFILE_UNSAFE$"):
        browser_profile_path(app, initialize=True)
    assert not (app / "browser-profile-selection.json").exists()


def test_default_configuration_is_compatible():
    with patch.dict(os.environ, {}, clear=True):
        assert configured_browser_profile() == "default"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO fixture")
def test_special_file_selection_is_rejected_before_open(tmp_path):
    os.mkfifo(tmp_path / "browser-profile-selection.json")
    with patch("services.applications.browser_profiles.os.open") as opened:
        with pytest.raises(ValueError, match="^BROWSER_PROFILE_SELECTION_INVALID$"):
            browser_profile_path(tmp_path)
        opened.assert_not_called()


@pytest.mark.parametrize("configured", ["review", ""])
def test_compose_preserves_selector_and_api_worker_validation(tmp_path, configured):
    from unittest.mock import Mock
    from apps.api.main import create_app
    from workers import main as worker_main
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI unavailable")
    environment = {**os.environ, "POSTGRES_PASSWORD": "synthetic-test-only",
        "MJOBS_CREDENTIAL_ENCRYPTION_KEY": "synthetic-test-only",
        "MJOBS_BROWSER_PROFILE": configured}
    env_file = tmp_path / "compose.env"
    env_file.write_text("")
    result = subprocess.run([docker, "compose", "--env-file", str(env_file), "-f",
        str(Path(__file__).resolve().parents[2] / "compose.yml"), "config", "--format", "json"],
        env=environment, capture_output=True, text=True, timeout=30, check=True)
    services = json.loads(result.stdout)["services"]
    for name in ("api", "worker", "source-worker", "document-worker", "ai-worker",
                 "company-discovery-worker", "scheduler"):
        assert services[name]["environment"]["MJOBS_BROWSER_PROFILE"] == configured
    if configured:
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": configured}):
            from tests.unit.test_configuration_surface_sustain_276 import _public_settings
            assert _public_settings(tmp_path)["browser_profile"] == {
                "name": "review", "scope": "PER_APPLICATION", "existing_sessions": "PINNED"}
    else:
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": ""}), \
                patch.object(worker_main, "Store") as factory:
            with pytest.raises(ValueError, match="^MJOBS_BROWSER_PROFILE_INVALID$"):
                worker_main.main()
            factory.assert_not_called()
            store = Mock()
            with pytest.raises(ValueError, match="^MJOBS_BROWSER_PROFILE_INVALID$"):
                create_app(store, "http://127.0.0.1:1", tmp_path)
            store.bind_workspace_mode.assert_not_called()
