"""Strict, side-effect-free configuration and public capability contracts."""
import json
from pathlib import Path

import pytest

from cli.notifications import main
from services.notifications.configuration import load_config, parse_config, read_token
from services.notifications.contract import NotificationError
from services.notifications.providers import capability_descriptors, create_provider


def config_data(tmp_path, **changes):
    return {
        "version": 1, "enabled": True, "profile_id": "fixture", "provider": "ntfy",
        "server_url": "https://notifications.example.test", "topic": "synthetic-topic",
        "state_path": str(tmp_path / "notifications.db"), **changes,
    }


def test_missing_or_disabled_configuration_has_no_side_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MJOBS_NOTIFICATION_CONFIG", raising=False)
    monkeypatch.delenv("MJOBS_DATABASE_URL", raising=False)
    config = load_config(environ={})
    assert not config.enabled and create_provider(config) is None
    assert main(["run", "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "DISABLED"
    assert list(tmp_path.iterdir()) == []
    assert not parse_config({"version": 1, "enabled": False}).enabled


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"enabled": "true"}, {"provider": "slack"},
    {"topic": "../other"}, {"topic": "a/b"}, {"topic": "secret\r\nOther"},
    {"topic": ""}, {"topic": "x" * 65}, {"profile_id": ""}, {"profile_id": []},
    {"state_path": "relative.db"}, {"state_path": None},
    {"server_url": "http://remote.example.test"},
    {"server_url": "http://localhost:8080", "allow_loopback_http": True},
    {"server_url": "http://192.168.1.2", "allow_loopback_http": True},
    {"server_url": "https://user:pass@example.test"},
    {"server_url": "https://example.test/topic"},
    {"server_url": "https://example.test/?token=secret"},
    {"server_url": "https://example.test/#secret"},
    {"server_url": "https://example.test/?"}, {"server_url": "https://example.test/#"},
    {"server_url": "https://example.test:99999"},
    {"server_url": "https://example.test\r\nBad: value"},
    {"server_url": "https://example.test\\other"},
    {"server_url": "https://example.test%40evil.test"},
    {"allow_loopback_http": 1}, {"poll_seconds": True}, {"poll_seconds": 1},
    {"batch_size": 201}, {"request_timeout": 31},
    {"token_env": "BAD\nNAME"}, {"token_env": "TOKEN", "token_file": "/some/file"},
    {"token_file": "relative.token"}, {"api_key": "unsupported"},
])
def test_invalid_configuration_is_rejected_without_private_diagnostics(tmp_path, change):
    with pytest.raises(NotificationError) as raised:
        parse_config(config_data(tmp_path, **change))
    assert str(raised.value) in {"NOTIFICATION_CONFIG_INVALID", "NOTIFICATION_PROVIDER_UNSUPPORTED"}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:8123", "http://[::1]:8123"])
def test_local_http_requires_explicit_literal_loopback_opt_in(tmp_path, url):
    with pytest.raises(NotificationError):
        parse_config(config_data(tmp_path, server_url=url))
    assert parse_config(config_data(tmp_path, server_url=url, allow_loopback_http=True)).server_url == url


def test_origin_is_canonical_and_binding_excludes_rotatable_credentials(tmp_path):
    config = parse_config(config_data(tmp_path, server_url="https://EXAMPLE.test:443/"))
    other = parse_config(config_data(tmp_path, server_url="https://example.test:443", token_env="NTFY_TOKEN"))
    assert config.server_url == "https://example.test:443"
    assert config.binding == other.binding
    assert config.binding != parse_config(config_data(tmp_path, topic="new-destination")).binding


def test_token_env_or_private_file_is_bounded_and_never_in_error(tmp_path):
    config = parse_config(config_data(tmp_path, token_env="NTFY_TOKEN"))
    assert read_token(config, environ={"NTFY_TOKEN": "tk_synthetic-value"}) == "tk_synthetic-value"
    for token in ("", "private\r\nAuthorization: injected", "abc def", "x" * 4097, 7):
        with pytest.raises(NotificationError, match="NOTIFICATION_CREDENTIAL_UNAVAILABLE"):
            read_token(config, environ={"NTFY_TOKEN": token})
    token_file = tmp_path / "ntfy.token"
    token_file.write_text("tk_file_fixture\n", encoding="ascii")
    file_config = parse_config(config_data(tmp_path, token_file=str(token_file)))
    assert read_token(file_config) == "tk_file_fixture"
    token_file.write_bytes(b"x" * 4097)
    with pytest.raises(NotificationError, match="NOTIFICATION_CREDENTIAL_UNAVAILABLE"):
        read_token(file_config)


def test_bad_or_oversized_private_config_never_echoes_content(tmp_path, capsys):
    path = tmp_path / "config.json"
    for content in ('{"secret": "DO_NOT_ECHO"', "DO_NOT_ECHO" * 4000):
        path.write_text(content, encoding="utf-8")
        assert main(["--config", str(path), "status"]) == 2
        output = capsys.readouterr().out
        assert "DO_NOT_ECHO" not in output
        assert str(path) not in output
        assert "NOTIFICATION_CONFIG_INVALID" in output


def test_capabilities_distinguish_server_delivery_and_unsupported_guarantees(capsys):
    descriptor, = capability_descriptors()
    assert descriptor["provider"] == "ntfy"
    assert descriptor["execution_surface"] == "server"
    assert "exactly_once_delivery" in descriptor["unsupported"]
    assert "device_delivery_receipt" in descriptor["unsupported"]
    assert main(["capabilities"]) == 0
    assert json.loads(capsys.readouterr().out)["providers"][0] == json.loads(json.dumps(descriptor))


def test_duplicate_configuration_keys_cannot_silently_enable_delivery(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"version":1,"enabled":false,"enabled":true}', encoding="utf-8")
    with pytest.raises(NotificationError, match="NOTIFICATION_CONFIG_INVALID"):
        load_config(path)
