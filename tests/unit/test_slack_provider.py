"""Slack incoming-webhook contract, credential privacy and bounded outcomes."""
from dataclasses import replace
import json
import logging

import httpx
import pytest

from services.notifications.configuration import parse_config
from services.notifications.events import event_from_row
from services.notifications.providers import capability_descriptors, create_provider


def config_data(tmp_path, **changes):
    return {
        "version": 1, "enabled": True, "provider": "slack", "profile_id": "fixture",
        "slack_team_id": "T0123456789", "slack_service_id": "B0123456789",
        "token_env": "SLACK_SYNTHETIC_TOKEN", "state_path": str(tmp_path / "slack.db"),
        **changes,
    }


def event():
    return event_from_row({
        "id": "private-operation-id", "kind": "APPLICATION_DRY_RUN", "state": "COMPLETED",
        "updated_at": "2026-11-15T01:00:00+00:00", "attempt_count": 1,
    })


def provider(tmp_path, handler, **config):
    parsed = parse_config(config_data(tmp_path, **config))
    return create_provider(parsed, transport=httpx.MockTransport(handler),
                          environ={"SLACK_SYNTHETIC_TOKEN": "synthetic_slack_webhook_secret"})


def test_webhook_posts_generic_plain_text_without_mention_parsing(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == (
            "https://hooks.slack.com/services/T0123456789/B0123456789/"
            "synthetic_slack_webhook_secret")
        body = json.loads(request.content)
        assert body == {
            "text": "Application dry-run completed. Open MJobs for details.",
            "mrkdwn": False,
        }
        assert "link_names" not in body
        assert b"private-operation-id" not in request.content
        return httpx.Response(200, text="ok")

    result = provider(tmp_path, handler).send(event())
    assert result.status == "DELIVERED" and result.code == "NOTIFICATION_SERVER_ACKNOWLEDGED"
    assert len(requests) == 1


def test_capabilities_report_slack_bounds_and_unsupported_surfaces():
    providers = capability_descriptors()
    descriptor = next(value for value in providers if value["provider"] == "slack")
    assert descriptor["execution_surface"] == "server"
    assert "mentions_disabled" in descriptor["guarantees"]
    assert "server_acknowledgement_validation" in descriptor["guarantees"]
    assert "exactly_once_delivery" in descriptor["unsupported"]
    assert "device_delivery_receipt" in descriptor["unsupported"]
    assert "attachments" in descriptor["unsupported"]


@pytest.mark.parametrize("status,code", [
    (401, "NOTIFICATION_AUTH_REJECTED"),
    (400, "NOTIFICATION_DESTINATION_REJECTED"),
    (403, "NOTIFICATION_DESTINATION_REJECTED"),
    (404, "NOTIFICATION_DESTINATION_REJECTED"),
])
def test_permanent_webhook_http_rejections_do_not_echo_bodies(tmp_path, status, code):
    result = provider(tmp_path, lambda _request: httpx.Response(
        status, content=b"PRIVATE_SLACK_ERROR")).send(event())
    assert result.status == "REJECTED" and result.code == code
    assert "PRIVATE_SLACK_ERROR" not in repr(result)


def test_rate_limit_preserves_retry_after_for_explicit_retry(tmp_path):
    result = provider(tmp_path, lambda _request: httpx.Response(
        429, headers={"Retry-After": "23"}, content=b"secret")).send(event())
    assert result.status == "REJECTED" and result.code == "NOTIFICATION_RATE_LIMITED"
    assert result.retry_after == 23
    assert "secret" not in repr(result)


@pytest.mark.parametrize("status", [201, 202, 204, 301, 302, 307, 308, 500, 503])
def test_uncertain_http_results_are_not_followed_or_retried(tmp_path, status):
    requests = []
    result = provider(tmp_path, lambda request: requests.append(request) or httpx.Response(
        status, headers={"Location": "https://attacker.invalid/"}, content=b"PRIVATE")).send(event())
    assert result.status == "UNKNOWN" and result.code == "NOTIFICATION_ACK_UNCONFIRMED"
    assert len(requests) == 1
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("response,code", [
    (lambda: httpx.Response(200, text="not-ok"), "NOTIFICATION_ACK_INVALID"),
    (lambda: httpx.Response(200, content=b"ok", headers={"Content-Type": "application/json"}),
     "NOTIFICATION_ACK_INVALID"),
    (lambda: httpx.Response(200, content=b"x" * 8193, headers={"Content-Type": "text/plain"}),
     "NOTIFICATION_ACK_TOO_LARGE"),
])
def test_success_acknowledgement_is_exact_and_bounded(tmp_path, response, code):
    result = provider(tmp_path, lambda _request: response()).send(event())
    assert result.status == "UNKNOWN" and result.code == code


def test_compressed_ack_is_rejected_without_decoding(tmp_path):
    class ForbiddenStream(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Compressed response must not be decoded")
        def close(self):
            pass

    response = httpx.Response(200, headers={
        "Content-Type": "text/plain", "Content-Encoding": "gzip",
    }, stream=ForbiddenStream())
    result = provider(tmp_path, lambda _request: response).send(event())
    assert result.status == "UNKNOWN"
    assert result.code == "NOTIFICATION_ACK_ENCODING_UNSUPPORTED"


@pytest.mark.parametrize("error,status,code", [
    (httpx.ConnectError, "REJECTED", "NOTIFICATION_NOT_CONNECTED"),
    (httpx.ConnectTimeout, "REJECTED", "NOTIFICATION_NOT_CONNECTED"),
    (httpx.ReadTimeout, "UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT"),
    (httpx.WriteTimeout, "UNKNOWN", "NOTIFICATION_DELIVERY_TIMEOUT"),
    (httpx.ReadError, "UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN"),
    (httpx.RemoteProtocolError, "UNKNOWN", "NOTIFICATION_TRANSPORT_UNCERTAIN"),
])
def test_transport_failures_are_sanitized_and_uncertain_after_send(tmp_path, error, status, code):
    def handler(request):
        raise error("PRIVATE_SLACK_TRANSPORT_ERROR", request=request)

    result = provider(tmp_path, handler).send(event())
    assert result.status == status and result.code == code
    assert "PRIVATE_SLACK_TRANSPORT_ERROR" not in repr(result)


def test_invalid_event_and_missing_secret_do_not_send(tmp_path):
    requests = []
    sender = provider(tmp_path, lambda request: requests.append(request))
    sender.environ = {}
    assert sender.send(event()).code == "NOTIFICATION_CREDENTIAL_UNAVAILABLE"
    assert sender.send(replace(event(), message="PRIVATE_APPLICANT_DATA")).status == "FAILED"
    assert requests == []


def test_httpx_request_log_redacts_the_webhook_path_secret(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = provider(tmp_path, lambda _request: httpx.Response(200, text="ok")).send(event())
    assert result.status == "DELIVERED"
    assert "synthetic_slack_webhook_secret" not in caplog.text
