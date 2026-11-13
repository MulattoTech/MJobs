"""Strict private configuration; owned diagnostics omit all destination/secret values."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from services.notifications.contract import NotificationError


CONFIG_ENV = "MJOBS_NOTIFICATION_CONFIG"
_FIELDS = {
    "version", "enabled", "provider", "profile_id", "server_url", "topic",
    "state_path", "token_env", "token_file", "allow_loopback_http",
    "poll_seconds", "batch_size", "request_timeout",
}
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = False
    provider: str = "ntfy"
    profile_id: str = ""
    server_url: str = ""
    topic: str = ""
    state_path: str = ""
    token_env: str = ""
    token_file: str = ""
    allow_loopback_http: bool = False
    poll_seconds: int = 10
    batch_size: int = 50
    request_timeout: int = 10

    @property
    def binding(self) -> str:
        value = [self.provider, self.profile_id, self.server_url, self.topic]
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _invalid():
    raise NotificationError("NOTIFICATION_CONFIG_INVALID")


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _invalid()
    return value


def _absolute_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        _invalid()
    if not Path(value).is_absolute():
        _invalid()
    return value


def parse_config(raw) -> NotificationConfig:
    if (type(raw) is not dict or set(raw) - _FIELDS
            or type(raw.get("version")) is not int or raw["version"] != 1
            or type(raw.get("enabled")) is not bool):
        _invalid()
    if not raw["enabled"]:
        return NotificationConfig()
    if raw.get("provider", "ntfy") != "ntfy":
        raise NotificationError("NOTIFICATION_PROVIDER_UNSUPPORTED")
    profile = raw.get("profile_id")
    topic = raw.get("topic")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        _invalid()
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic):
        _invalid()
    allow_http = raw.get("allow_loopback_http", False)
    if type(allow_http) is not bool:
        _invalid()
    url = raw.get("server_url")
    if (not isinstance(url, str) or len(url) > 2048 or not url.isascii()
            or any(ord(c) < 33 or c == "\\" for c in url)):
        _invalid()
    if "?" in url or "#" in url:
        _invalid()
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or (port is not None and not 1 <= port <= 65535)):
            _invalid()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                _invalid()
        if parsed.scheme != "https":
            if not (parsed.scheme == "http" and allow_http and ip is not None and ip.is_loopback):
                _invalid()
        authority = f"[{host.lower()}]" if ip is not None and ip.version == 6 else host.lower()
        if port is not None:
            authority += f":{port}"
        url = f"{parsed.scheme}://{authority}"
    except (ValueError, TypeError):
        _invalid()
    token_env = raw.get("token_env", "")
    token_file = raw.get("token_file", "")
    if not isinstance(token_env, str) or not isinstance(token_file, str) or (token_env and token_file):
        _invalid()
    if token_env and not _ENV.fullmatch(token_env):
        _invalid()
    if token_file:
        token_file = _absolute_path(token_file)
    return NotificationConfig(
        enabled=True, profile_id=profile, server_url=url, topic=topic,
        state_path=_absolute_path(raw.get("state_path")),
        token_env=token_env, token_file=token_file, allow_loopback_http=allow_http,
        poll_seconds=_integer(raw.get("poll_seconds", 10), 2, 300),
        batch_size=_integer(raw.get("batch_size", 50), 1, 200),
        request_timeout=_integer(raw.get("request_timeout", 10), 2, 30),
    )



def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def load_config(path=None, *, environ=None) -> NotificationConfig:
    environment = os.environ if environ is None else environ
    selected = path if path is not None else environment.get(CONFIG_ENV)
    if not selected:
        return NotificationConfig()
    try:
        with Path(selected).open("rb") as handle:
            data = handle.read(32769)
        if len(data) > 32768:
            _invalid()
        raw = json.loads(data, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError, TypeError):
        raise NotificationError("NOTIFICATION_CONFIG_INVALID") from None
    return parse_config(raw)


def read_token(config: NotificationConfig, *, environ=None) -> str:
    environment = os.environ if environ is None else environ
    token = ""
    try:
        if config.token_env:
            token = environment.get(config.token_env, "")
        elif config.token_file:
            with Path(config.token_file).open("rb") as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            token = raw.decode("ascii").strip()
        if config.token_env or config.token_file:
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", token):
                raise ValueError()
    except (OSError, UnicodeError, ValueError, TypeError):
        raise NotificationError("NOTIFICATION_CREDENTIAL_UNAVAILABLE") from None
    return token
