"""ntfy's HTTP contract, privacy boundaries, acknowledgement and failure semantics."""
from dataclasses import replace
import json
import logging

import httpx
import pytest

from services.notifications.configuration import parse_config
from services.notifications.events import event_from_row
from services.notifications.providers.ntfy import NtfyProvider
from tests.unit.test_notification_configuration import config_data


def event():
    return event_from_row({
        "id": "op-private-id", "kind": "APPLICATION_DRY_RUN", "state": "COMPLETED",
        "updated_at": "2026-11-13T01:00:00+00:00", "attempt_count": 1,
    })


def acknowledgement(request, **changes):
    sent = json.loads(request.content)
    return {
        "id": "fixture-message-id", "event": "message", "topic": sent["topic"],
        "message": sent["message"], "title": sent["title"], **changes,
    }


def provider(tmp_path, handler, **config):
    value = parse_config(config_data(tmp_path, **config))
    return NtfyProvider(value, transport=httpx.MockTransport(handler))


def test_publish_uses_one_fixed_root_post_and_generic_utf8_text(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("MJOBS_SYNTHETIC_TOKEN", "tk_synthetic")
    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://notifications.example.test/"
        assert request.headers["Authorization"] == "Bearer tk_synthetic"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {
            "topic": "synthetic-topic", "title": "MJobs activity",
            "message": "Application dry-run completed. Open MJobs for details.",
        }
        assert b"op-private-id" not in request.content
        assert b"submitted" not in request.content
        return httpx.Response(200, json=acknowledgement(request))
    result = provider(tmp_path, handler, token_env="MJOBS_SYNTHETIC_TOKEN").send(event())
    assert result.status == "DELIVERED" and result.message_id == "fixture-message-id"
    assert len(requests) == 1


@pytest.mark.parametrize("change", [
    {"topic": "other"}, {"event": "open"}, {"message": "different"},
    {"title": "other"}, {"id": ""}, {"id": None}, {"id": "x" * 129},
])
def test_invalid_matching_ack_is_unknown_not_success_or_retryable(tmp_path, change):
    result = provider(tmp_path, lambda request: httpx.Response(
        200, json=acknowledgement(request, **change))).send(event())
    assert result.status == "UNKNOWN" and result.code == "NOTIFICATION_ACK_INVALID"


@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 308, 400, 404, 500, 503])
def test_other_http_results_are_uncertain_and_never_followed_or_retried(tmp_path, status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.invalid/private"},
                              content=b"DO_NOT_ECHO")
    result = provider(tmp_path, handler).send(event())
    assert result.status == "UNKNOWN" and len(requests) == 1
    assert "DO_NOT_ECHO" not in repr(result)


@pytest.mark.parametrize("status,code", [
    (401, "NOTIFICATION_AUTH_REJECTED"), (403, "NOTIFICATION_AUTH_REJECTED"),
    (429, "NOTIFICATION_RATE_LIMITED"),
])
def test_definite_rejections_preserve_rate_limit(tmp_path, status, code):
    result = provider(tmp_path, lambda request: httpx.Response(
        status, headers={"Retry-After": "120"}, content=b"SECRET")).send(event())
    assert result.status == "REJECTED" and result.code == code
    assert result.retry_after == (120 if status == 429 else 0)
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize("response,code", [
    (lambda: httpx.Response(200, content=b"not json", headers={"Content-Type": "application/json"}),
     "NOTIFICATION_ACK_INVALID"),
    (lambda: httpx.Response(200, json=[]), "NOTIFICATION_ACK_INVALID"),
    (lambda: httpx.Response(200, content=b"x" * 65537, headers={"Content-Type": "application/json"}),
     "NOTIFICATION_ACK_TOO_LARGE"),
    (lambda: httpx.Response(200, content=b"{}", headers={"Content-Type": "text/html"}),
     "NOTIFICATION_ACK_INVALID"),
])
def test_response_validation_is_bounded_and_sanitized(tmp_path, response, code):
    result = provider(tmp_path, lambda request: response()).send(event())
    assert result.status == "UNKNOWN" and result.code == code


@pytest.mark.parametrize("error,status,code", [
    (httpx.ConnectError, "REJECTED", "NOTIFICATION_NOT_CONNECTED"),
    (httpx.ConnectTimeout, "REJECTED", "NOTIFICATION_NOT_CONNECTED"),
    (httpx.ReadTimeout, "UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT"),
    (httpx.WriteTimeout, "UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT"),
    (httpx.ReadError, "UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN"),
    (httpx.WriteError, "UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN"),
    (httpx.RemoteProtocolError, "UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN"),
])
def test_only_transport_connect_phase_is_safe_to_retry(tmp_path, error, status, code):
    def handler(request):
        raise error("PRIVATE_ERROR_TEXT", request=request)
    result = provider(tmp_path, handler).send(event())
    assert result.status == status and result.code == code
    assert "PRIVATE_ERROR_TEXT" not in repr(result)


def test_missing_credentials_and_invalid_event_make_zero_requests(tmp_path):
    requests = []
    sender = provider(tmp_path, lambda request: requests.append(request), token_env="ABSENT_NTFY_TOKEN")
    sender.environ = {}
    assert sender.send(event()).code == "NOTIFICATION_CREDENTIAL_UNAVAILABLE"
    assert sender.send(replace(event(), message="PRIVATE_APPLICANT_DATA")).status == "FAILED"
    assert requests == []


def test_production_client_disables_redirects_proxies_and_keeps_verification(tmp_path, monkeypatch):
    seen = {}
    real_client = httpx.Client
    def client(**kwargs):
        seen.update(kwargs)
        return real_client(**kwargs)
    monkeypatch.setattr("services.notifications.providers.ntfy.httpx.Client", client)
    result = provider(tmp_path, lambda request: httpx.Response(
        200, json=acknowledgement(request))).send(event())
    assert result.status == "DELIVERED"
    assert seen["follow_redirects"] is False and seen["trust_env"] is False
    assert seen.get("verify", True) is True


def test_compressed_ack_is_rejected_before_decoding_or_iteration(tmp_path):
    class ForbiddenStream(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Compressed response must never be decoded")
        def close(self):
            pass
    def handler(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip",
        }, stream=ForbiddenStream())
    result = provider(tmp_path, handler).send(event())
    assert result.status == "UNKNOWN"
    assert result.code == "NOTIFICATION_ACK_ENCODING_UNSUPPORTED"


def test_httpx_info_logging_never_contains_topic_token_or_private_event(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MJOBS_PRIVATE_TEST_TOKEN", "tk_private_bearer_fixture")
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = provider(
            tmp_path, lambda request: httpx.Response(200, json=acknowledgement(request)),
            topic="private_topic_not_for_logs", token_env="MJOBS_PRIVATE_TEST_TOKEN",
        ).send(event())
    assert result.status == "DELIVERED"
    assert any(record.name == "httpx" for record in caplog.records)
    for secret in ("private_topic_not_for_logs", "tk_private_bearer_fixture", "op-private-id"):
        assert secret not in caplog.text
