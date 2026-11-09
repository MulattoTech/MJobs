"""Original MJ-REQ-143 indicators and guards through the production receipt consumer.

Only the HTTP transport is synthetic. The production MockClient and _receipt
perform their normal request, origin, identity, capture and observed-page checks.
The separate browser acceptance covers persistence and mounted UI readback.
"""
from datetime import datetime, timedelta

import httpx
import pytest

from services.applications.mock_adapter import _receipt
from services.discovery import pipeline

ORIGIN = "http://127.0.0.1:8765"
APPLICATION = {"id": "confirmation-143"}
REQUISITION = "fixture-role-143"
REFERENCE = "MOCK-confirmation-143"
TIMESTAMP = "2026-11-09T15:00:00+00:00"
CONFIRMATION_URL = ORIGIN + "/ats/submit/" + REQUISITION
RECEIPT_URL = ORIGIN + "/ats/receipts/" + APPLICATION["id"]


def valid_receipt(text=None):
    return {
        "application_id": APPLICATION["id"],
        "requisition": REQUISITION,
        "receipt": REFERENCE,
        "timestamp": TIMESTAMP,
        "confirmation_url": CONFIRMATION_URL,
        "confirmation_text": text or f"Thank you for applying. {REFERENCE}",
        "reference_number": REFERENCE,
        "submission_count": 1,
    }


@pytest.fixture
def receipt_client(monkeypatch):
    reply = {"payload": valid_receipt()}
    requests = []

    def handle(request):
        requests.append((request.method, str(request.url), request.content))
        if reply.get("failure") == "read":
            raise httpx.ReadError("Synthetic receipt read failure", request=request)
        if reply.get("failure") == "timeout":
            raise httpx.ReadTimeout("Synthetic receipt timeout", request=request)
        if reply.get("failure") == "invalid-json":
            return httpx.Response(200, content=b"{", request=request)
        if reply.get("failure") == "http":
            return httpx.Response(503, json={"detail": "Unavailable"}, request=request)
        return httpx.Response(200, json=reply["payload"], request=request)

    # Preserve the real client and consumer; replace only network I/O.
    monkeypatch.setattr(
        pipeline, "SourceTransport", lambda check: httpx.MockTransport(handle),
    )
    client = pipeline.MockClient(ORIGIN)
    try:
        yield client, reply
    finally:
        client.close()
        # No submission, unrelated lookup or foreign-origin fetch is permissible.
        assert requests == [("GET", RECEIPT_URL, b"")]


@pytest.mark.parametrize(
    "indicator",
    [
        pytest.param("Thank you for applying", id="thank-you-for-applying"),
        pytest.param("Application received", id="application-received"),
        pytest.param("We've received your application", id="received-your-application"),
        pytest.param("Confirmation ID", id="confirmation-id"),
        pytest.param("Application reference", id="application-reference"),
    ],
)
def test_each_required_indicator_preserves_all_four_observed_capture_fields(
    receipt_client, indicator,
):
    client, reply = receipt_client
    # Each case contains one original requirement indicator, with no fallback
    # "Thank you" or "Application received" phrase hiding another detector.
    text = f"{indicator}: {REFERENCE}"
    reply["payload"] = valid_receipt(text)

    result = _receipt(
        client, APPLICATION, REQUISITION,
        observed_url=CONFIRMATION_URL,
        observed_text=text,
        observed_reference=REFERENCE,
    )

    assert result is not None
    assert result["timestamp"] == "2026-11-09T15:00:00+00:00"
    assert datetime.fromisoformat(result["timestamp"]).utcoffset() == timedelta(0)
    assert result["confirmation_url"] == ORIGIN + "/ats/submit/fixture-role-143"
    assert result["confirmation_text"] == text
    assert result["reference_number"] == "MOCK-confirmation-143"
    assert result["receipt"] == result["reference_number"]
    assert result["application_id"] == "confirmation-143"
    assert result["requisition"] == "fixture-role-143"


def test_case_and_whitespace_matching_preserves_original_text_and_timestamp(
    receipt_client,
):
    client, reply = receipt_client
    text = f"  tHaNk\tYOU\nfor   ApPlYiNg.  {REFERENCE}\n"
    reply["payload"] = valid_receipt(text)
    reply["payload"]["timestamp"] = "2026-11-09T15:00:00Z"

    result = _receipt(
        client, APPLICATION, REQUISITION,
        observed_url=CONFIRMATION_URL,
        observed_text=f"tHaNk YOU for ApPlYiNg. {REFERENCE}",
        observed_reference=REFERENCE,
    )

    assert result is not None
    assert result["confirmation_text"] == text
    assert result["timestamp"] == "2026-11-09T15:00:00Z"
    assert result["confirmation_url"] == CONFIRMATION_URL
    assert result["reference_number"] == REFERENCE


@pytest.mark.parametrize(
    "field", ["timestamp", "confirmation_url", "confirmation_text", "reference_number"],
)
def test_missing_required_capture_field_cannot_be_a_verified_receipt(
    receipt_client, field,
):
    client, reply = receipt_client
    del reply["payload"][field]

    # Mandatory receipt validation also protects the read-only recovery consumer,
    # where there is no newly observed page to provide a second rejection reason.
    assert _receipt(client, APPLICATION, REQUISITION) is None


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"application_id": "another-application"}, id="wrong-application"),
        pytest.param({"requisition": "another-role"}, id="wrong-requisition"),
        pytest.param({"timestamp": None}, id="null-timestamp"),
        pytest.param({"timestamp": 143}, id="non-string-timestamp"),
        pytest.param({"timestamp": "not-a-timestamp"}, id="invalid-timestamp"),
        pytest.param({"timestamp": "2026-11-09T15:00:00"}, id="timezone-free-timestamp"),
        pytest.param(
            {"confirmation_url": "https://confirmation.example.invalid/thanks"},
            id="foreign-confirmation-origin",
        ),
        pytest.param(
            {"confirmation_url": "http://fixture-user@127.0.0.1:8765/ats/submit/fixture-role-143"},
            id="credential-bearing-confirmation-url",
        ),
        pytest.param(
            {"confirmation_url": "http://127.0.0.1:8766/ats/submit/fixture-role-143"},
            id="different-confirmation-port",
        ),
        pytest.param(
            {"confirmation_url": "https://127.0.0.1:8765/ats/submit/fixture-role-143"},
            id="different-confirmation-scheme",
        ),
        pytest.param({"confirmation_url": None}, id="null-confirmation-url"),
        pytest.param({"confirmation_text": ""}, id="empty-confirmation-text"),
        pytest.param({"confirmation_text": ["Application received"]}, id="non-string-text"),
        pytest.param(
            {"confirmation_text": f"Application received. {REFERENCE}" + " x" * 5001},
            id="oversized-confirmation-text",
        ),
        pytest.param(
            {"confirmation_text": f"Your application is still being processed. {REFERENCE}"},
            id="unrecognized-confirmation-text",
        ),
        pytest.param(
            {"confirmation_text": "Thank you for applying."},
            id="reference-missing-from-confirmation-text",
        ),
        pytest.param({"reference_number": None}, id="null-reference"),
        pytest.param({"reference_number": "MOCK-another-reference"}, id="receipt-reference-mismatch"),
        pytest.param(
            {
                "receipt": "OTHER-confirmation-143",
                "reference_number": "OTHER-confirmation-143",
                "confirmation_text": "Application received. OTHER-confirmation-143",
            },
            id="foreign-receipt-namespace",
        ),
    ],
)
def test_invalid_receipt_evidence_is_not_verified(receipt_client, changes):
    client, reply = receipt_client
    reply["payload"].update(changes)

    assert _receipt(client, APPLICATION, REQUISITION) is None


@pytest.mark.parametrize(
    "observed_changes",
    [
        pytest.param(
            {"observed_url": ORIGIN + "/ats/submit/another-role"},
            id="observed-url-mismatch",
        ),
        pytest.param(
            {"observed_text": f"Application received. {REFERENCE}"},
            id="observed-text-mismatch",
        ),
        pytest.param(
            {"observed_reference": "MOCK-another-reference"},
            id="observed-reference-mismatch",
        ),
    ],
)
def test_receipt_must_match_the_actually_observed_page(receipt_client, observed_changes):
    client, reply = receipt_client
    observed = {
        "observed_url": CONFIRMATION_URL,
        "observed_text": reply["payload"]["confirmation_text"],
        "observed_reference": REFERENCE,
    }
    observed.update(observed_changes)

    assert _receipt(client, APPLICATION, REQUISITION, **observed) is None


@pytest.mark.parametrize("payload", [None, [], "Application received"])
def test_non_object_response_is_not_a_verified_receipt(receipt_client, payload):
    client, reply = receipt_client
    reply["payload"] = payload

    assert _receipt(client, APPLICATION, REQUISITION) is None


@pytest.mark.parametrize("failure", ["read", "timeout", "invalid-json", "http"])
def test_unavailable_or_invalid_receipt_response_cannot_confirm(receipt_client, failure):
    client, reply = receipt_client
    reply["failure"] = failure

    assert _receipt(client, APPLICATION, REQUISITION) is None

