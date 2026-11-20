"""Bounded Slack incoming-webhook delivery of generic activity summaries."""
from __future__ import annotations

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


class _WebhookSecretLogFilter(logging.Filter):
    """Redact the webhook URL secret from HTTPX's normal request log record."""

    def __init__(self, secret):
        super().__init__()
        self.secret = secret

    def filter(self, record):
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        if self.secret in message:
            record.msg = message.replace(self.secret, "[REDACTED]")
            record.args = ()
        return True


class SlackProvider:
    def __init__(self, config, *, transport=None, environ=None):
        if not config.enabled or config.provider != "slack":
            raise NotificationError("NOTIFICATION_PROVIDER_DISABLED")
        self.config = config
        self.transport = transport
        self.environ = environ

    @staticmethod
    def capabilities():
        return ProviderCapabilities(
            provider="slack", execution_surface="server",
            events=("operation_completed", "operation_failed", "operation_cancelled",
                    "operation_review_required"),
            authentication=("operator_managed_incoming_webhook_token_env_or_file",),
            guarantees=("generic_text_only", "bounded_request", "no_redirects",
                        "server_acknowledgement_validation", "mentions_disabled"),
            unsupported=("device_delivery_receipt", "exactly_once_delivery", "attachments",
                         "blocks", "thread_routing", "channel_override", "message_readback",
                         "message_edit_or_delete", "email", "direct_application_submission",
                         "historical_event_stream"),
        )

    def send(self, event):
        from services.notifications.events import message_for

        message = message_for(event.operation_kind, event.state)
        if message is None or message != event.message or len(message) > 1900:
            return DeliveryResult("FAILED", "NOTIFICATION_EVENT_INVALID")
        try:
            secret = read_token(self.config, environ=self.environ)
        except NotificationError:
            return DeliveryResult("REJECTED", "NOTIFICATION_CREDENTIAL_UNAVAILABLE")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", secret):
            return DeliveryResult("REJECTED", "NOTIFICATION_CREDENTIAL_UNAVAILABLE")

        # Incoming webhook secrets are URL path credentials. Construct only the
        # standard Slack host/path; never accept a caller-controlled destination.
        url = (f"https://hooks.slack.com/services/{self.config.slack_team_id}/"
               f"{self.config.slack_service_id}/{secret}")
        headers = {
            "Accept": "text/plain",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "MJobs-Notifications/1.0",
        }
        deadline = _Deadline(self.config.request_timeout)
        transport = self.transport if self.transport is not None else SourceTransport(deadline)
        secret_filter = _WebhookSecretLogFilter(secret)
        httpx_logger = logging.getLogger("httpx")
        httpx_logger.addFilter(secret_filter)
        try:
            with httpx.Client(
                transport=transport, timeout=self.config.request_timeout,
                follow_redirects=False, trust_env=False, headers=headers,
            ) as client:
                deadline()
                with client.stream("POST", url, json={"text": message, "mrkdwn": False}) as response:
                    if response.status_code == 429:
                        wait = retry_after_seconds(
                            response.headers.get("retry-after"), default=60, minimum=1,
                        )
                        return DeliveryResult("REJECTED", "NOTIFICATION_RATE_LIMITED",
                                              retry_after=wait)
                    if response.status_code == 401:
                        return DeliveryResult("REJECTED", "NOTIFICATION_AUTH_REJECTED")
                    if response.status_code in (400, 403, 404):
                        return DeliveryResult("REJECTED", "NOTIFICATION_DESTINATION_REJECTED")
                    if response.status_code != 200:
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_UNCONFIRMED")
                    if response.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_ENCODING_UNSUPPORTED")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type != "text/plain":
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_INVALID")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=4096):
                        deadline()
                        body.extend(chunk)
                        if len(body) > 8192:
                            return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_TOO_LARGE")
                    deadline()
                    acknowledgement = bytes(body).strip(b" \t\r\n")
                    if acknowledgement == b"ok":
                        return DeliveryResult("DELIVERED", "NOTIFICATION_SERVER_ACKNOWLEDGED")
                    if acknowledgement in {
                        b"invalid_token", b"no_active_hooks", b"no_service", b"no_service_id",
                        b"no_team", b"team_disabled",
                    }:
                        return DeliveryResult("REJECTED", "NOTIFICATION_AUTH_REJECTED")
                    return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_INVALID")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return DeliveryResult("REJECTED", "NOTIFICATION_NOT_CONNECTED")
        except (_DeadlineExpired, httpx.TimeoutException):
            # The webhook may have stored the message before timeout/cancellation.
            return DeliveryResult("UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT")
        except httpx.HTTPError:
            return DeliveryResult("UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN")
        finally:
            httpx_logger.removeFilter(secret_filter)
