"""MJ-REQ-275 ordinary settings must include safe notification controls."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.api.main import create_app
from packages.persistence.store import Store


def _public_settings(root: Path) -> dict:
    store = Store(f"sqlite:///{(root / 'configuration.db').as_posix()}")
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    try:
        with patch("apps.api.main.create_provider",
                   side_effect=AssertionError("settings readback started a provider")), \
                patch("apps.api.main.enqueue_operation",
                      side_effect=AssertionError("settings readback queued delivery")), \
                patch("socket.create_connection",
                      side_effect=AssertionError("settings readback opened a socket")):
            app = create_app(
                store,
                "http://127.0.0.1:1",
                root,
                operation_mode="FIXTURE",
            )
            route = next(
                route for route in app.routes if getattr(route, "path", None) == "/settings")
            return route.endpoint()
    finally:
        store.engine.dispose()


def test_notifications_default_to_disabled_in_app_delivery_without_external_side_effects(tmp_path):
    with patch.dict(os.environ, {}, clear=True):
        settings = _public_settings(tmp_path)

    assert settings["notifications"] == {"enabled": False, "delivery": "IN_APP"}
    assert settings["external_submission_enabled"] is False


@pytest.mark.parametrize(("configured", "expected"), (("0", False), ("1", True)))
def test_notifications_accept_only_explicit_binary_opt_in(tmp_path, configured, expected):
    with patch.dict(os.environ, {"MJOBS_NOTIFICATIONS_ENABLED": configured}, clear=True):
        settings = _public_settings(tmp_path)

    assert settings["notifications"] == {"enabled": expected, "delivery": "IN_APP"}
    assert set(settings["notifications"]) == {"enabled", "delivery"}


@pytest.mark.parametrize("configured", ("", "true", "yes", "2", "-1", " 1 "))
def test_invalid_notification_configuration_fails_closed(tmp_path, configured):
    with patch.dict(os.environ, {"MJOBS_NOTIFICATIONS_ENABLED": configured}, clear=True), \
            pytest.raises(ValueError, match="^MJOBS_NOTIFICATIONS_ENABLED_INVALID$"):
        _public_settings(tmp_path)


@pytest.mark.parametrize(("configured", "expected"), ((None, False), ("0", False), ("1", True)))
def test_compose_notification_configuration_reaches_public_readback(tmp_path, configured, expected):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is unavailable; API configuration tests still run")
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.pop("MJOBS_NOTIFICATIONS_ENABLED", None)
    environment["POSTGRES_PASSWORD"] = "configuration-test-only"
    environment["MJOBS_CREDENTIAL_ENCRYPTION_KEY"] = "configuration-test-only"
    if configured is not None:
        environment["MJOBS_NOTIFICATIONS_ENABLED"] = configured
    env_file = tmp_path / "compose.env"
    env_file.write_text("", encoding="utf-8")
    rendered = subprocess.run(
        [docker, "compose", "--env-file", str(env_file), "-f", str(root / "compose.yml"),
         "config", "--format", "json"],
        env=environment, capture_output=True, text=True, timeout=30, check=True,
    )
    api_environment = json.loads(rendered.stdout)["services"]["api"]["environment"]
    with patch.dict(os.environ, {
        "MJOBS_NOTIFICATIONS_ENABLED": api_environment["MJOBS_NOTIFICATIONS_ENABLED"],
    }, clear=True):
        settings = _public_settings(tmp_path)
    assert settings["notifications"] == {"enabled": expected, "delivery": "IN_APP"}
    assert settings["external_submission_enabled"] is False
