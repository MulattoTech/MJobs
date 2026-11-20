"""Discord webhook contract, secret handling and bounded outcomes."""
from dataclasses import replace
import json
import logging

import httpx
import pytest

from services.notifications.configuration import parse_config
from services.notifications.events import event_from_row
from services.notifications.providers import create_provider


def config_data(tmp_path, **changes):
    return {
        "version": 1, "enabled": True, "provider": "discord", "profile_id": "fixture",
        "webhook_id": "12345678901234567", "token_env": "DISCORD_SYNTHETIC_TOKEN",
        "state_path": str(tmp_path / "discord.db"), **changes,
    }


def event():
    return event_from_row({
        "id": "private-operation-id", "kind": "APPLICATION_DRY_RUN", "state": "COMPLETED",
        "updated_at": "2026-11-13T01:00:00+00:00", "attempt_count": 1,
    })


def provider(tmp_path, handler, **config):
    parsed = parse_config(config_data(tmp_path, **config))
    return create_provider(parsed, transport=httpx.MockTransport(handler),
                          environ={"DISCORD_SYNTHETIC_TOKEN": "synthetic_webhook_token"})


def test_webhook_sends_only_generic_content_with_mentions_disabled(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == (
            "https://discord.com/api/webhooks/12345678901234567/"
            "synthetic_webhook_token?wait=true")
        body = json.loads(request.content)
        assert body == {
            "content": "Application dry-run completed. Open MJobs for details.",
            "allowed_mentions": {"parse": []},
        }
        assert b"private-operation-id" not in request.content
        assert b"PRIVATE" not in request.content
        return httpx.Response(204)

    result = provider(tmp_path, handler).send(event())
    assert result.status == "DELIVERED" and result.code == "NOTIFICATION_SERVER_ACCEPTED"
    assert len(requests) == 1


def test_create_provider_selects_discord_and_accepts_json_ack(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"id": "123456789012345678"})

    result = provider(tmp_path, handler).send(event())
    assert result.status == "DELIVERED"
    assert result.message_id == "123456789012345678"


@pytest.mark.parametrize("status,code", [
    (401, "NOTIFICATION_DESTINATION_REJECTED"),
    (403, "NOTIFICATION_DESTINATION_REJECTED"),
    (404, "NOTIFICATION_DESTINATION_REJECTED"),
    (429, "NOTIFICATION_RATE_LIMITED"),
])
def test_definite_http_rejections_are_sanitized_and_rate_limit_is_preserved(tmp_path, status, code):
    result = provider(tmp_path, lambda _request: httpx.Response(
        status, headers={"Retry-After": "17"}, content=b"SECRET_DO_NOT_ECHO")).send(event())
    assert result.status == "REJECTED" and result.code == code
    assert result.retry_after == (17 if status == 429 else 0)
    assert "SECRET_DO_NOT_ECHO" not in repr(result)


@pytest.mark.parametrize("status", [201, 202, 301, 302, 307, 308, 400, 500, 503])
def test_unconfirmed_http_results_are_not_followed_or_retried(tmp_path, status):
    requests = []
    result = provider(tmp_path, lambda request: requests.append(request) or httpx.Response(
        status, headers={"Location": "https://attacker.invalid/"}, content=b"SECRET")).send(event())
    assert result.status == "UNKNOWN" and result.code == "NOTIFICATION_ACK_UNCONFIRMED"
    assert len(requests) == 1
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize("content,headers,code", [
    (b"bad", {"Content-Type": "application/json"}, "NOTIFICATION_ACK_INVALID"),
    (b"[]", {"Content-Type": "application/json"}, "NOTIFICATION_ACK_INVALID"),
    (b"{}", {"Content-Type": "text/html"}, "NOTIFICATION_ACK_INVALID"),
    (b"{}", {"Content-Type": "application/json", "Content-Encoding": "gzip"},
     "NOTIFICATION_ACK_ENCODING_UNSUPPORTED"),
    (b"x" * 65537, {"Content-Type": "application/json"}, "NOTIFICATION_ACK_TOO_LARGE"),
])
def test_acknowledgements_are_bounded_and_validated(tmp_path, content, headers, code):
    if code == "NOTIFICATION_ACK_ENCODING_UNSUPPORTED":
        class ForbiddenStream(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError("Compressed response must not be decoded")
            def close(self):
                pass
        response = httpx.Response(200, headers=headers, stream=ForbiddenStream())
    else:
        response = httpx.Response(200, content=content, headers=headers)
    result = provider(tmp_path, lambda _request: response).send(event())
    assert result.status == "UNKNOWN" and result.code == code


@pytest.mark.parametrize("body", [{"id": "bad"}, {"id": "x" * 19}, [], {"id": None}])
def test_invalid_success_acknowledgement_is_unknown(tmp_path, body):
    result = provider(tmp_path, lambda _request: httpx.Response(200, json=body)).send(event())
    assert result.status == "UNKNOWN" and result.code == "NOTIFICATION_ACK_INVALID"


@pytest.mark.parametrize("error,status,code", [
    (httpx.ConnectError, "REJECTED", "NOTIFICATION_NOT_CONNECTED"),
    (httpx.ConnectTimeout, "REJECTED", "NOTIFICATION_NOT_CONNECTED"),
    (httpx.ReadTimeout, "UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT"),
    (httpx.WriteTimeout, "UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT"),
    (httpx.ReadError, "UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN"),
    (httpx.RemoteProtocolError, "UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN"),
])
def test_transport_failures_are_classified_without_private_diagnostics(tmp_path, error, status, code):
    def handler(request):
        raise error("PRIVATE_TRANSPORT_DETAIL", request=request)

    result = provider(tmp_path, handler).send(event())
    assert result.status == status and result.code == code
    assert "PRIVATE_TRANSPORT_DETAIL" not in repr(result)


def test_invalid_event_or_missing_credential_makes_no_request(tmp_path):
    requests = []
    sender = provider(tmp_path, lambda request: requests.append(request))
    sender.environ = {}
    assert sender.send(event()).code == "NOTIFICATION_CREDENTIAL_UNAVAILABLE"
    assert sender.send(replace(event(), message="PRIVATE_APPLICANT_DATA")).status == "FAILED"
    assert requests == []


def test_production_client_disables_redirects_proxies_and_keeps_tls_verification(tmp_path, monkeypatch):
    seen = {}
    real_client = httpx.Client

    def client(**kwargs):
        seen.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("services.notifications.providers.discord.httpx.Client", client)
    result = provider(tmp_path, lambda _request: httpx.Response(204)).send(event())
    assert result.status == "DELIVERED"
    assert seen["follow_redirects"] is False and seen["trust_env"] is False
    assert seen.get("verify", True) is True


def test_httpx_info_logging_does_not_expose_webhook_token(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = provider(tmp_path, lambda _request: httpx.Response(204)).send(event())
    assert result.status == "DELIVERED"
    assert "synthetic_webhook_token" not in caplog.text
