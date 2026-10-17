"""Breezy dispatch consumes the foundation's review-only static analyzer."""
import json

import pytest

from services.applications.form_review_providers import (
    analyze_application_form,
    normalize_application_route,
)


ROUTE = "https://acme.breezy.hr/p/389eb1b8c3ef-job"
METADATA = {"provider": "breezy", "tenant": "acme", "requisition": "389eb1b8c3ef"}


@pytest.mark.parametrize("as_bytes", [False, True])
def test_breezy_public_dispatch_preserves_adapter_review_and_value_exclusion(as_bytes):
    route = normalize_application_route("breezy", ROUTE, route_metadata=METADATA)
    assert route is not None
    assert route["provider"] == "breezy"
    assert route["tenant"] == "acme"
    assert route["requisition"] == "389eb1b8c3ef"

    from services.applications.adapters.breezy import BreezyApplicationAdapter

    markup = ('<form><label for="email">PRIVATE_PROMPT</label>'
              '<input id="email" name="email" type="email" required '
              'value="PRIVATE_VALUE"></form>')
    html = markup.encode() if as_bytes else markup
    report = analyze_application_form("BREEZY", html, route_url=ROUTE)
    assert report == BreezyApplicationAdapter().analyze_form(html, route_url=route["url"])
    assert report["status"] == "REVIEW_REQUIRED"
    assert report["reason"] == "MANUAL_REVIEW_REQUIRED"
    assert report["fields"][0]["id"] == "email"
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("override", [
    {"provider": "lever"}, {"tenant": "other"}, {"requisition": "deadbeef0000"},
])
def test_breezy_stored_identity_mismatch_is_rejected(override):
    assert normalize_application_route(
        "breezy", ROUTE, route_metadata={**METADATA, **override},
    ) is None
