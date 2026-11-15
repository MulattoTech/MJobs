"""Bounded Discord webhook delivery of generic MJobs activity summaries."""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from services.discovery.sources.http_transport import SourceTransport
from services.discovery.sources.retry_after import retry_after_seconds
from services.notifications.configuration import read_token
from services.notifications.contract import DeliveryResult, NotificationError, ProviderCapabilities


class _DeadlineExpired(RuntimeError):
    pass


class _Deadline:
    def __init__(self, seconds):
        self.ends = time.monotonic() + seconds

    def __call__(self):
        if time.monotonic() >= self.ends:
            raise _DeadlineExpired()


class _WebhookTokenLogFilter(logging.Filter):
    """Redact the path credential from HTTPX's normal request log record."""

    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        if self.token in message:
            record.msg = message.replace(self.token, "[REDACTED]")
            record.args = ()
        return True


class DiscordProvider:
    def __init__(self, config, *, transport=None, environ=None):
        if not config.enabled or config.provider != "discord":
            raise NotificationError("NOTIFICATION_PROVIDER_DISABLED")
        self.config = config
        self.transport = transport
        self.environ = environ

    @staticmethod
    def capabilities():
        return ProviderCapabilities(
            provider="discord", execution_surface="server",
            events=("operation_completed", "operation_failed", "operation_cancelled",
                    "operation_review_required"),
            authentication=("operator_managed_webhook_token_env_or_file",),
            guarantees=("generic_text_only", "bounded_request", "no_redirects",
                        "server_acknowledgement_only", "mentions_disabled"),
            unsupported=("device_delivery_receipt", "exactly_once_delivery", "attachments",
                         "embeds", "user_or_role_mentions", "thread_routing", "email",
                         "direct_application_submission", "historical_event_stream"),
        )

    def send(self, event):
        from services.notifications.events import message_for

        message = message_for(event.operation_kind, event.state)
        if message is None or message != event.message or len(message) > 1900:
            return DeliveryResult("FAILED", "NOTIFICATION_EVENT_INVALID")
        try:
            token = read_token(self.config, environ=self.environ)
        except NotificationError:
            return DeliveryResult("REJECTED", "NOTIFICATION_CREDENTIAL_UNAVAILABLE")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", token):
            return DeliveryResult("REJECTED", "NOTIFICATION_CREDENTIAL_UNAVAILABLE")

        # Discord webhook tokens are URL path credentials. Keep the destination
        # host and route fixed, disable redirects/proxies, and never echo the URL.
        url = (f"https://discord.com/api/webhooks/{self.config.webhook_id}/"
               f"{token}?wait=true")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "MJobs-Notifications/1.0",
        }
        deadline = _Deadline(self.config.request_timeout)
        transport = self.transport if self.transport is not None else SourceTransport(deadline)
        token_filter = _WebhookTokenLogFilter(token)
        httpx_logger = logging.getLogger("httpx")
        httpx_logger.addFilter(token_filter)
        try:
            with httpx.Client(
                transport=transport, timeout=self.config.request_timeout,
                follow_redirects=False, trust_env=False, headers=headers,
            ) as client:
                deadline()
                with client.stream("POST", url, json={
                    "content": message,
                    "allowed_mentions": {"parse": []},
                }) as response:
                    if response.status_code in (401, 403, 404):
                        return DeliveryResult("REJECTED", "NOTIFICATION_DESTINATION_REJECTED")
                    if response.status_code == 429:
                        wait = retry_after_seconds(
                            response.headers.get("retry-after"), default=60, minimum=1,
                        )
                        return DeliveryResult("REJECTED", "NOTIFICATION_RATE_LIMITED",
                                              retry_after=wait)
                    if response.status_code not in (200, 204):
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_UNCONFIRMED")
                    if response.status_code == 204:
                        deadline()
                        return DeliveryResult("DELIVERED", "NOTIFICATION_SERVER_ACCEPTED")
                    if response.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_ENCODING_UNSUPPORTED")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type != "application/json":
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_INVALID")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=4096):
                        deadline()
                        body.extend(chunk)
                        if len(body) > 65536:
                            return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_TOO_LARGE")
                    deadline()
                    try:
                        acknowledgement = json.loads(body)
                    except (ValueError, UnicodeError):
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_INVALID")
                    if (type(acknowledgement) is not dict
                            or not isinstance(acknowledgement.get("id"), str)
                            or not re.fullmatch(r"[0-9]{17,20}", acknowledgement["id"])):
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_INVALID")
                    return DeliveryResult(
                        "DELIVERED", "NOTIFICATION_SERVER_ACCEPTED", acknowledgement["id"])
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return DeliveryResult("REJECTED", "NOTIFICATION_NOT_CONNECTED")
        except (_DeadlineExpired, httpx.TimeoutException):
            # The request may have been accepted before cancellation or timeout.
            return DeliveryResult("UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT")
        except httpx.HTTPError:
            return DeliveryResult("UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN")
        finally:
            httpx_logger.removeFilter(token_filter)
