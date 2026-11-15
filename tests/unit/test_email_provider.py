"""Bounded SMTP-over-verified-TLS notification contract tests."""
from email import policy
from email.message import EmailMessage
from dataclasses import replace
import smtplib
import ssl
import threading

import pytest

from services.notifications.configuration import parse_config
from services.notifications.contract import DeliveryResult, NotificationError
from services.notifications.events import event_from_row
from services.notifications.providers.email import EmailProvider


def config_data(tmp_path, **changes):
    return {
        "version": 1, "enabled": True, "provider": "email", "profile_id": "fixture",
        "smtp_host": "smtp.example.test", "smtp_sender": "notify@example.test",
        "smtp_recipient": "owner@example.test", "smtp_username": "mjobs-app",
        "smtp_password_env": "SMTP_SYNTHETIC_PASSWORD",
        "state_path": str(tmp_path / "email.db"), **changes,
    }


def event(**changes):
    return event_from_row({
        "id": "private-operation-id", "kind": "APPLICATION_DRY_RUN", "state": "COMPLETED",
        "updated_at": "2026-11-13T01:00:00+00:00", "attempt_count": 1, **changes,
    })


class FakeSMTP:
    instances = []
    login_error = None
    send_result = None
    send_error = None
    exit_error = None
    during_send = None

    def __init__(self, host, port, *, timeout, context, deadline):
        self.host, self.port, self.timeout = host, port, timeout
        self.context, self.deadline = context, deadline
        self.login_args = None
        self.send_args = None
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if type(self).exit_error:
            raise type(self).exit_error
        return False

    def login(self, username, password):
        self.login_args = (username, password)
        if type(self).login_error:
            raise type(self).login_error
        return 235, b"authenticated"

    def send_message(self, message, *, from_addr, to_addrs):
        self.send_args = (message, from_addr, to_addrs)
        if type(self).during_send:
            type(self).during_send()
        if type(self).send_error:
            raise type(self).send_error
        return type(self).send_result or {}


def make_provider(tmp_path, *, config_changes=None, environ=None, stopped=None):
    FakeSMTP.instances = []
    FakeSMTP.login_error = None
    FakeSMTP.send_result = None
    FakeSMTP.send_error = None
    FakeSMTP.exit_error = None
    FakeSMTP.during_send = None
    config = parse_config(config_data(tmp_path, **(config_changes or {})))
    return EmailProvider(config, smtp_factory=FakeSMTP,
                         environ=(environ if environ is not None else {
                             "SMTP_SYNTHETIC_PASSWORD": "synthetic-password-42",
                         }), cancellation_event=stopped)


def test_sends_one_generic_message_to_exact_recipient_over_verified_implicit_tls(tmp_path):
    result = make_provider(tmp_path).send(event())
    smtp = FakeSMTP.instances[0]
    assert result == DeliveryResult("DELIVERED", "NOTIFICATION_SMTP_ACCEPTED")
    assert (smtp.host, smtp.port) == ("smtp.example.test", 465)
    assert 0 < smtp.timeout <= 10
    assert smtp.context.verify_mode == ssl.CERT_REQUIRED
    assert smtp.context.check_hostname is True
    assert smtp.login_args == ("mjobs-app", "synthetic-password-42")
    message, sender, recipients = smtp.send_args
    assert isinstance(message, EmailMessage)
    assert message.policy == policy.SMTP
    assert message["From"] == "notify@example.test"
    assert message["To"] == "owner@example.test"
    assert message["Subject"] == "MJobs activity"
    assert message.get_content() == "Application dry-run completed. Open MJobs for details.\n"
    assert sender == "notify@example.test"
    assert recipients == ["owner@example.test"]
    raw = message.as_bytes()
    assert b"private-operation-id" not in raw
    assert b"PRIVATE" not in raw


def test_capabilities_name_only_the_supported_smtp_surface():
    capabilities = EmailProvider.capabilities()
    assert capabilities.provider == "email"
    assert "smtp_ssl_implicit_tls_port_465" in capabilities.authentication
    assert "smtp_auth_username_password" in capabilities.authentication
    assert "verified_tls_hostname" in capabilities.guarantees
    assert "smtp_server_acceptance_only" in capabilities.guarantees
    assert "inbox_delivery_receipt" in capabilities.unsupported
    assert "multiple_recipients" in capabilities.unsupported
    assert "exactly_once_delivery" in capabilities.unsupported


def test_provider_rejects_invalid_event_or_missing_password_without_connecting(tmp_path):
    provider = make_provider(tmp_path, environ={})
    assert provider.send(replace(event(), message="private user content")).status == "FAILED"
    assert provider.send(event()).code == "NOTIFICATION_CREDENTIAL_UNAVAILABLE"
    assert FakeSMTP.instances == []


def test_auth_and_exact_recipient_rejections_are_sanitized_and_not_unknown(tmp_path):
    provider = make_provider(tmp_path)
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(535, b"secret diagnostic")
    result = provider.send(event())
    assert result == DeliveryResult("REJECTED", "NOTIFICATION_AUTH_REJECTED")
    assert "secret diagnostic" not in result.code

    provider = make_provider(tmp_path)
    FakeSMTP.login_error = None
    FakeSMTP.send_result = {"owner@example.test": (550, b"private recipient detail")}
    result = provider.send(event())
    assert result == DeliveryResult("REJECTED", "NOTIFICATION_RECIPIENT_REJECTED")
    assert "private recipient detail" not in result.code


@pytest.mark.parametrize("error", [ConnectionRefusedError("private host"), ssl.SSLCertVerificationError("private cert")])
def test_connection_or_tls_rejection_happens_before_delivery(tmp_path, error):
    class FailingFactory:
        def __init__(self, *_args, **_kwargs):
            raise error

    config = parse_config(config_data(tmp_path))
    result = EmailProvider(config, smtp_factory=FailingFactory,
                           environ={"SMTP_SYNTHETIC_PASSWORD": "synthetic-password-42"}).send(event())
    assert result.status == "REJECTED"
    assert result.code in {"NOTIFICATION_NOT_CONNECTED", "NOTIFICATION_TLS_REJECTED"}
    assert "private" not in result.code


@pytest.mark.parametrize("error", [TimeoutError("read timeout"), smtplib.SMTPServerDisconnected("closed")])
def test_uncertain_failure_during_message_submission_is_not_retryable(tmp_path, error):
    provider = make_provider(tmp_path)
    FakeSMTP.send_error = error
    result = provider.send(event())
    assert result.status == "UNKNOWN"
    assert result.code in {"NOTIFICATION_DELIVERY_TIMEOUT", "NOTIFICATION_SMTP_UNCERTAIN"}


def test_failure_after_acceptance_acknowledgement_is_conservatively_unknown(tmp_path):
    provider = make_provider(tmp_path)
    FakeSMTP.exit_error = smtplib.SMTPServerDisconnected("close failed")
    result = provider.send(event())
    assert result == DeliveryResult("UNKNOWN", "NOTIFICATION_SMTP_UNCERTAIN")


def test_cancellation_before_connection_and_during_submit_have_distinct_outcomes(tmp_path):
    stopped = threading.Event()
    stopped.set()
    result = make_provider(tmp_path, stopped=stopped).send(event())
    assert result == DeliveryResult("REJECTED", "NOTIFICATION_CANCELLED")
    assert FakeSMTP.instances == []

    stopped.clear()
    provider = make_provider(tmp_path, stopped=stopped)
    FakeSMTP.during_send = stopped.set
    result = provider.send(event())
    assert result == DeliveryResult("UNKNOWN", "NOTIFICATION_CANCELLED_UNCERTAIN")


def test_refused_or_unsupported_auth_is_sanitized(tmp_path):
    provider = make_provider(tmp_path)
    FakeSMTP.login_error = smtplib.SMTPNotSupportedError("private AUTH detail")
    result = provider.send(event())
    assert result == DeliveryResult("REJECTED", "NOTIFICATION_AUTH_UNSUPPORTED")


def test_non_email_configuration_cannot_construct_email_provider(tmp_path):
    config = parse_config({
        "version": 1, "enabled": True, "provider": "ntfy", "profile_id": "fixture",
        "server_url": "https://notifications.example.test", "topic": "topic",
        "state_path": str(tmp_path / "ntfy.db"),
    })
    with pytest.raises(NotificationError, match="NOTIFICATION_PROVIDER_DISABLED"):
        EmailProvider(config)
