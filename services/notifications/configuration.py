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
    "webhook_id", "slack_team_id", "slack_service_id",
    "smtp_host", "smtp_sender", "smtp_recipient", "smtp_username",
    "smtp_password_env", "smtp_password_file",
    "state_path", "token_env", "token_file", "allow_loopback_http",
    "poll_seconds", "batch_size", "request_timeout",
}
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SMTP_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z")
_SMTP_USERNAME = re.compile(r"[!-~]{1,320}\Z")


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = False
    provider: str = "ntfy"
    profile_id: str = ""
    server_url: str = ""
    topic: str = ""
    webhook_id: str = ""
    slack_team_id: str = ""
    slack_service_id: str = ""
    smtp_host: str = ""
    smtp_sender: str = ""
    smtp_recipient: str = ""
    smtp_username: str = ""
    smtp_password_env: str = ""
    smtp_password_file: str = ""
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
        if self.provider == "discord":
            value.append(self.webhook_id)
        elif self.provider == "slack":
            value.extend((self.slack_team_id, self.slack_service_id))
        elif self.provider == "email":
            value = [self.provider, self.profile_id, self.smtp_host, "465", self.smtp_sender,
                     self.smtp_recipient, self.smtp_username]
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


def _smtp_host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or not value.isascii():
        _invalid()
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or any(
            char in value for char in "/\\@?#[]"):
        _invalid()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        labels = value.rstrip(".").split(".")
        if (value.endswith(".") or any(not _SMTP_LABEL.fullmatch(label) for label in labels)):
            _invalid()
        return value.lower()


def _email_address(value):
    if (not isinstance(value, str) or not value.isascii() or len(value) > 254
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or value.count("@") != 1):
        _invalid()
    local, domain = value.rsplit("@", 1)
    labels = domain.split(".")
    if (len(local) > 64 or not _EMAIL_LOCAL.fullmatch(local)
            or len(labels) < 2 or any(not _SMTP_LABEL.fullmatch(label) for label in labels)):
        _invalid()
    return f"{local}@{domain.lower()}"


def parse_config(raw) -> NotificationConfig:
    if (type(raw) is not dict or set(raw) - _FIELDS
            or type(raw.get("version")) is not int or raw["version"] != 1
            or type(raw.get("enabled")) is not bool):
        _invalid()
    if not raw["enabled"]:
        return NotificationConfig()
    provider = raw.get("provider", "ntfy")
    if not isinstance(provider, str) or provider not in {"ntfy", "discord", "slack", "email"}:
        raise NotificationError("NOTIFICATION_PROVIDER_UNSUPPORTED")
    profile = raw.get("profile_id")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        _invalid()
    topic = ""
    url = ""
    allow_http = False
    webhook_id = ""
    slack_team_id = ""
    slack_service_id = ""
    smtp_host = smtp_sender = smtp_recipient = smtp_username = ""
    smtp_password_env = smtp_password_file = ""
    if provider == "ntfy":
        if any(key in raw for key in (
                "webhook_id", "slack_team_id", "slack_service_id", "smtp_host", "smtp_sender",
                "smtp_recipient", "smtp_username", "smtp_password_env", "smtp_password_file")):
            _invalid()
        topic = raw.get("topic")
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
    else:
        if "server_url" in raw or "topic" in raw or "allow_loopback_http" in raw:
            _invalid()
        if provider == "discord":
            if any(key in raw for key in (
                    "slack_team_id", "slack_service_id", "smtp_host", "smtp_sender",
                    "smtp_recipient", "smtp_username", "smtp_password_env", "smtp_password_file")):
                _invalid()
            webhook_id = raw.get("webhook_id")
            if not isinstance(webhook_id, str) or not re.fullmatch(r"[0-9]{17,20}", webhook_id):
                _invalid()
        elif provider == "slack":
            if any(key in raw for key in (
                    "webhook_id", "smtp_host", "smtp_sender", "smtp_recipient", "smtp_username",
                    "smtp_password_env", "smtp_password_file")):
                _invalid()
            slack_team_id = raw.get("slack_team_id")
            slack_service_id = raw.get("slack_service_id")
            if (not isinstance(slack_team_id, str)
                    or not re.fullmatch(r"T[A-Z0-9]{1,32}", slack_team_id)
                    or not isinstance(slack_service_id, str)
                    or not re.fullmatch(r"B[A-Z0-9]{1,32}", slack_service_id)):
                _invalid()
        else:
            if any(key in raw for key in (
                    "webhook_id", "slack_team_id", "slack_service_id", "token_env", "token_file")):
                _invalid()
            smtp_host = _smtp_host(raw.get("smtp_host"))
            smtp_sender = _email_address(raw.get("smtp_sender"))
            smtp_recipient = _email_address(raw.get("smtp_recipient"))
            smtp_username = raw.get("smtp_username")
            if not isinstance(smtp_username, str) or not _SMTP_USERNAME.fullmatch(smtp_username):
                _invalid()
            smtp_password_env = raw.get("smtp_password_env", "")
            smtp_password_file = raw.get("smtp_password_file", "")
            if (not isinstance(smtp_password_env, str) or not isinstance(smtp_password_file, str)
                    or bool(smtp_password_env) == bool(smtp_password_file)):
                _invalid()
            if smtp_password_env and not _ENV.fullmatch(smtp_password_env):
                _invalid()
            if smtp_password_file:
                smtp_password_file = _absolute_path(smtp_password_file)
    token_env = raw.get("token_env", "") if provider != "email" else ""
    token_file = raw.get("token_file", "") if provider != "email" else ""
    if not isinstance(token_env, str) or not isinstance(token_file, str) or (token_env and token_file):
        _invalid()
    if provider in {"discord", "slack"} and not (token_env or token_file):
        _invalid()
    if token_env and not _ENV.fullmatch(token_env):
        _invalid()
    if token_file:
        token_file = _absolute_path(token_file)
    return NotificationConfig(
        enabled=True, provider=provider, profile_id=profile, server_url=url, topic=topic,
        webhook_id=webhook_id, slack_team_id=slack_team_id, slack_service_id=slack_service_id,
        smtp_host=smtp_host, smtp_sender=smtp_sender, smtp_recipient=smtp_recipient,
        smtp_username=smtp_username, smtp_password_env=smtp_password_env,
        smtp_password_file=smtp_password_file,
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


def read_smtp_password(config: NotificationConfig, *, environ=None) -> str:
    environment = os.environ if environ is None else environ
    try:
        if config.smtp_password_env:
            password = environment.get(config.smtp_password_env, "")
        elif config.smtp_password_file:
            with Path(config.smtp_password_file).open("rb") as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            password = raw.decode("ascii")
            if password.endswith("\r\n"):
                password = password[:-2]
            elif password.endswith("\n"):
                password = password[:-1]
        else:
            password = ""
        if (not isinstance(password, str) or not 1 <= len(password) <= 1024
                or not password.isascii()
                or any(ord(char) < 32 or ord(char) == 127 for char in password)):
            raise ValueError()
        return password
    except (OSError, UnicodeError, ValueError, TypeError):
        raise NotificationError("NOTIFICATION_CREDENTIAL_UNAVAILABLE") from None
