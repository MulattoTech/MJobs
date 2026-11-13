"""Bounded ntfy JSON publishing. HTTP acknowledgement is not device receipt.

Only fixed generic activity text leaves MJobs. No redirects, environment proxy,
attachment, application submission, or blind retry is supported.
"""
from __future__ import annotations

import json
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

    def remaining_seconds(self):
        self()
        return max(.001, self.ends - time.monotonic())


class NtfyProvider:
    def __init__(self, config, *, transport=None, environ=None):
        if not config.enabled or config.provider != "ntfy":
            raise NotificationError("NOTIFICATION_PROVIDER_DISABLED")
        self.config = config
        self.transport = transport
        self.environ = environ

    @staticmethod
    def capabilities():
        return ProviderCapabilities(
            provider="ntfy", execution_surface="server",
            events=("operation_completed", "operation_failed", "operation_cancelled",
                    "operation_review_required"),
            authentication=("none_for_operator_selected_topic", "bearer_token_env_or_file"),
            guarantees=("generic_text_only", "bounded_request", "no_redirects",
                        "server_acknowledgement_validation"),
            unsupported=("device_delivery_receipt", "exactly_once_delivery", "attachments",
                         "email", "direct_application_submission", "historical_event_stream"),
        )

    def send(self, event):
        # Rebuild from the allowlisted kind/state rather than trusting journal message text.
        from services.notifications.events import message_for
        message = message_for(event.operation_kind, event.state)
        if message is None or message != event.message or len(message.encode("utf-8")) > 4096:
            return DeliveryResult("FAILED", "NOTIFICATION_EVENT_INVALID")
        try:
            token = read_token(self.config, environ=self.environ)
        except NotificationError:
            return DeliveryResult("REJECTED", "NOTIFICATION_CREDENTIAL_UNAVAILABLE")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "MJobs-Notifications/1.0",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        deadline = _Deadline(self.config.request_timeout)
        transport = self.transport if self.transport is not None else SourceTransport(deadline)
        try:
            with httpx.Client(
                transport=transport, timeout=self.config.request_timeout,
                follow_redirects=False, trust_env=False, headers=headers,
            ) as client:
                deadline()
                with client.stream(
                    "POST", self.config.server_url + "/",
                    json={"topic": self.config.topic, "title": "MJobs activity", "message": message},
                ) as response:
                    if response.status_code in (401, 403):
                        return DeliveryResult("REJECTED", "NOTIFICATION_AUTH_REJECTED")
                    if response.status_code == 429:
                        wait = retry_after_seconds(
                            response.headers.get("retry-after"), default=60, minimum=1,
                        )
                        return DeliveryResult("REJECTED", "NOTIFICATION_RATE_LIMITED",
                                              retry_after=wait)
                    if response.status_code != 200:
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_UNCONFIRMED")
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
                        ack = json.loads(body)
                    except (ValueError, UnicodeError):
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_INVALID")
                    if (type(ack) is not dict or ack.get("event") != "message"
                            or ack.get("topic") != self.config.topic
                            or ack.get("message") != message
                            or ack.get("title") != "MJobs activity"
                            or not isinstance(ack.get("id"), str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ack["id"])):
                        return DeliveryResult("UNKNOWN", "NOTIFICATION_ACK_INVALID")
                    return DeliveryResult("DELIVERED", "NOTIFICATION_SERVER_ACKNOWLEDGED", ack["id"])
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # HTTPX's connect phase fails before any HTTP request bytes are sent.
            return DeliveryResult("REJECTED", "NOTIFICATION_NOT_CONNECTED")
        except (_DeadlineExpired, httpx.TimeoutException):
            return DeliveryResult("UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT")
        except httpx.HTTPError:
            return DeliveryResult("UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN")
