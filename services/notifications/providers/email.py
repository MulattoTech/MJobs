"""Bounded generic email notifications over authenticated implicit TLS only."""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
import smtplib
import ssl
import time

from services.notifications.configuration import read_smtp_password
from services.notifications.contract import DeliveryResult, NotificationError, ProviderCapabilities


SMTP_SSL_PORT = 465


class _DeadlineExpired(RuntimeError):
    pass


class _Cancelled(RuntimeError):
    pass


class _Deadline:
    def __init__(self, seconds, cancellation_event=None):
        self.ends = time.monotonic() + seconds
        self.cancellation_event = cancellation_event

    def remaining_seconds(self):
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            raise _Cancelled()
        remaining = self.ends - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExpired()
        return max(.001, remaining)


class _DeadlineSMTP_SSL(smtplib.SMTP_SSL):
    """Reset the socket timeout to the remaining provider budget per protocol I/O."""

    def __init__(self, host, port, *, timeout, context, deadline):
        self._deadline = deadline
        super().__init__(host, port, timeout=timeout, context=context)

    def send(self, data):
        if self.sock is not None:
            self.sock.settimeout(self._deadline.remaining_seconds())
        return super().send(data)

    def getreply(self):
        if self.sock is not None:
            self.sock.settimeout(self._deadline.remaining_seconds())
        return super().getreply()


class EmailProvider:
    def __init__(self, config, *, smtp_factory=None, environ=None, cancellation_event=None):
        if not config.enabled or config.provider != "email":
            raise NotificationError("NOTIFICATION_PROVIDER_DISABLED")
        self.config = config
        self.smtp_factory = smtp_factory or _DeadlineSMTP_SSL
        self.environ = environ
        self.cancellation_event = cancellation_event

    @staticmethod
    def capabilities():
        return ProviderCapabilities(
            provider="email", execution_surface="server",
            events=("operation_completed", "operation_failed", "operation_cancelled",
                    "operation_review_required"),
            authentication=("smtp_ssl_implicit_tls_port_465", "smtp_auth_username_password"),
            guarantees=("verified_tls_hostname", "generic_plain_text", "single_configured_recipient",
                        "bounded_protocol_io", "smtp_server_acceptance_only"),
            unsupported=("plaintext_smtp", "starttls_downgrade", "multiple_recipients", "attachments",
                         "html", "inbox_delivery_receipt", "message_readback", "exactly_once_delivery",
                         "direct_application_submission", "historical_event_stream"),
        )

    def send(self, event):
        from services.notifications.events import message_for

        message = message_for(event.operation_kind, event.state)
        if message is None or message != event.message or len(message.encode("utf-8")) > 512:
            return DeliveryResult("FAILED", "NOTIFICATION_EVENT_INVALID")
        try:
            password = read_smtp_password(self.config, environ=self.environ)
        except NotificationError:
            return DeliveryResult("REJECTED", "NOTIFICATION_CREDENTIAL_UNAVAILABLE")

        outgoing = EmailMessage(policy=policy.SMTP)
        outgoing["From"] = self.config.smtp_sender
        outgoing["To"] = self.config.smtp_recipient
        outgoing["Subject"] = "MJobs activity"
        outgoing.set_content(message)

        deadline = _Deadline(self.config.request_timeout, self.cancellation_event)
        submitting = False
        try:
            deadline.remaining_seconds()
            context = ssl.create_default_context()
            with self.smtp_factory(
                self.config.smtp_host, SMTP_SSL_PORT,
                timeout=deadline.remaining_seconds(), context=context, deadline=deadline,
            ) as smtp:
                deadline.remaining_seconds()
                smtp.login(self.config.smtp_username, password)
                deadline.remaining_seconds()
                submitting = True
                refused = smtp.send_message(
                    outgoing, from_addr=self.config.smtp_sender,
                    to_addrs=[self.config.smtp_recipient],
                )
                deadline.remaining_seconds()
                if not isinstance(refused, dict):
                    return DeliveryResult("UNKNOWN", "NOTIFICATION_SMTP_ACK_UNCONFIRMED")
                if refused:
                    return DeliveryResult("REJECTED", "NOTIFICATION_RECIPIENT_REJECTED")
            return DeliveryResult("DELIVERED", "NOTIFICATION_SMTP_ACCEPTED")
        except _Cancelled:
            if submitting:
                return DeliveryResult("UNKNOWN", "NOTIFICATION_CANCELLED_UNCERTAIN")
            return DeliveryResult("REJECTED", "NOTIFICATION_CANCELLED")
        except _DeadlineExpired:
            if submitting:
                return DeliveryResult("UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT")
            return DeliveryResult("REJECTED", "NOTIFICATION_NOT_CONNECTED")
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult("REJECTED", "NOTIFICATION_AUTH_REJECTED")
        except smtplib.SMTPNotSupportedError:
            return DeliveryResult("REJECTED", "NOTIFICATION_AUTH_UNSUPPORTED")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            return DeliveryResult("REJECTED", "NOTIFICATION_SMTP_REJECTED")
        except (TimeoutError, smtplib.SMTPServerDisconnected, ssl.SSLError, OSError, smtplib.SMTPException):
            if submitting:
                return DeliveryResult("UNKNOWN", "NOTIFICATION_SMTP_UNCERTAIN")
            return DeliveryResult("REJECTED", "NOTIFICATION_NOT_CONNECTED")
