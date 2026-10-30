"""MJ-REQ-275 ordinary settings must include safe notification controls."""

import os
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
