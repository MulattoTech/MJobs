"""Fail-closed inspection evidence contract for extension ATS adapters."""
from __future__ import annotations

import json

from services.applications.form_review_providers import analyze_application_form
from services.applications.provider_registry import ApplicationAdapterRegistry


ROUTE = "https://fixture.invalid/jobs/123"
PRIVATE_PROMPT = "PRIVATE_INSPECTION_PROMPT_225"
PRIVATE_VALUE = "PRIVATE_INSPECTION_VALUE_225"


class _FixtureAdapter:
    def __init__(self, analysis):
        self.analysis = analysis

    def normalize_route(self, route):
        return dict(route)

    def analyze_form(self, _html, *, route_url):
        assert route_url == ROUTE
        return self.analysis

    @staticmethod
    def requires_human_review(_report):
        return False


def _registry(analysis):
    return ApplicationAdapterRegistry([
        ("fixture_app", lambda: _FixtureAdapter(analysis)),
    ])


def test_custom_adapter_missing_inspection_fields_fails_closed_and_valid_control_works():
    malformed = analyze_application_form(
        "fixture_app",
        f"<form><label>{PRIVATE_PROMPT}</label><input value='{PRIVATE_VALUE}'></form>",
        route_url=ROUTE,
        registry=_registry({
            "status": "PASS",
            "fields": None,
            "prompt": PRIVATE_PROMPT,
            "value": PRIVATE_VALUE,
        }),
    )

    assert malformed == {
        "status": "REVIEW_REQUIRED",
        "reason": "ANALYSIS_FAILED",
        "fields": [],
    }
    assert PRIVATE_PROMPT not in json.dumps(malformed)
    assert PRIVATE_VALUE not in json.dumps(malformed)

    valid = analyze_application_form(
        "fixture_app",
        f"<form><label>{PRIVATE_PROMPT}</label><input id='email' name='email' "
        f"type='email' value='{PRIVATE_VALUE}'></form>",
        route_url=ROUTE,
        registry=_registry({
            "status": "PASS",
            "fields": [{
                "id": "email",
                "name": "email",
                "input_type": "email",
                "required": True,
                "prompt": PRIVATE_PROMPT,
                "value": PRIVATE_VALUE,
            }],
        }),
    )

    assert valid == {
        "status": "ANALYZED",
        "reason": None,
        "fields": [{
            "id": "email",
            "name": "email",
            "type": "email",
            "required": True,
            "review_reason": None,
        }],
    }
    assert PRIVATE_PROMPT not in json.dumps(valid)
    assert PRIVATE_VALUE not in json.dumps(valid)
    assert "submission" not in valid
    assert "confirmation" not in valid
