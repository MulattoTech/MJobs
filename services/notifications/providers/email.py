"""Bounded generic email notifications over authenticated implicit TLS only."""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
import select
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
        return remaining


class _DeadlineSMTP_SSL(smtplib.SMTP_SSL):
    """Apply one operation deadline across connect, TLS, and SMTP reply parsing."""

    _CANCELLATION_POLL_SECONDS = .1

    def __init__(self, host, port, *, timeout, context, deadline):
        self._deadline = deadline
        super().__init__(host, port, timeout=timeout, context=context)

    def _get_socket(self, host, port, timeout):
        raw_socket = smtplib.SMTP._get_socket(
            self, host, port, self._deadline.remaining_seconds(),
        )
        tls_socket = None
        try:
            raw_socket.settimeout(self._deadline.remaining_seconds())
            tls_socket = self.context.wrap_socket(
                raw_socket, server_hostname=self._host, do_handshake_on_connect=False,
            )
            self._perform_tls_handshake(tls_socket)
            return tls_socket
        except BaseException:
            (tls_socket or raw_socket).close()
            raise

    def _wait_for_socket(self, sock, *, want_read, want_write):
        remaining = self._deadline.remaining_seconds()
        timeout = min(remaining, self._CANCELLATION_POLL_SECONDS)
        readable, writable, _ = select.select(
            [sock] if want_read else [], [sock] if want_write else [], [], timeout,
        )
        self._deadline.remaining_seconds()
        return bool(readable or writable)

    def _perform_tls_handshake(self, sock):
        sock.setblocking(False)
        while True:
            self._deadline.remaining_seconds()
            try:
                sock.do_handshake()
                sock.settimeout(self._deadline.remaining_seconds())
                return
            except ssl.SSLWantReadError:
                self._wait_for_socket(sock, want_read=True, want_write=False)
            except ssl.SSLWantWriteError:
                self._wait_for_socket(sock, want_read=False, want_write=True)

    def _receive_byte(self):
        while True:
            remaining = self._deadline.remaining_seconds()
            timeout = remaining
            if self._deadline.cancellation_event is not None:
                timeout = min(remaining, self._CANCELLATION_POLL_SECONDS)
            self.sock.settimeout(timeout)
            try:
                return self.sock.recv(1)
            except TimeoutError:
                self._deadline.remaining_seconds()
            except ssl.SSLWantReadError:
                self._wait_for_socket(self.sock, want_read=True, want_write=False)
            except ssl.SSLWantWriteError:
                self._wait_for_socket(self.sock, want_read=False, want_write=True)

    def _read_reply_line(self):
        line = bytearray()
        try:
            while len(line) <= smtplib._MAXLINE:
                byte = self._receive_byte()
                if not byte:
                    self.close()
                    raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
                line.extend(byte)
                if byte == b"\n":
                    if len(line) > smtplib._MAXLINE:
                        self.close()
                        raise smtplib.SMTPResponseException(500, "Line too long.")
                    return bytes(line)
        except (_Cancelled, _DeadlineExpired):
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise smtplib.SMTPServerDisconnected(
                "Connection unexpectedly closed: " + str(exc),
            ) from None
        self.close()
        raise smtplib.SMTPResponseException(500, "Line too long.")

    def send(self, data):
        if self.sock is not None:
            try:
                self.sock.settimeout(self._deadline.remaining_seconds())
            except (_Cancelled, _DeadlineExpired):
                self.close()
                raise
        return super().send(data)

    def getreply(self):
        if self.sock is None:
            raise smtplib.SMTPServerDisconnected("please run connect() first")
        response = []
        while True:
            line = self._read_reply_line()
            if self.debuglevel > 0:
                self._print_debug("reply:", repr(line))
            response.append(line[4:].strip(b" \t\r\n"))
            code = line[:3]
            try:
                error_code = int(code)
            except ValueError:
                error_code = -1
                break
            if line[3:4] != b"-":
                break
        message = b"\n".join(response)
        if self.debuglevel > 0:
            self._print_debug("reply: retcode (%s); Msg: %a" % (error_code, message))
        return error_code, message


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
