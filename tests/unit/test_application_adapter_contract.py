"""Registry-wide, value-free contracts for application adapters.

These tests exercise only local route normalization and supplied markup.  They
must never fetch a provider page, launch a browser, upload a document, fill a
live form, submit an application, or claim confirmation.
"""
from __future__ import annotations

import json
from subprocess import Popen
from unittest.mock import patch

import pytest

from services.applications.form_review_providers import analyze_application_form
from services.applications.provider_registry import (
    capabilities,
    get_provider,
    normalize_route,
    providers,
)


CANONICAL_ROUTES = {
    "ashby": "https://jobs.ashbyhq.com/example/engineering-123",
    "bamboohr": "https://acme.bamboohr.com/careers/12345",
    "breezy": (
        "https://acme.breezy.hr/p/"
        "123e4567-e89b-12d3-a456-426614174000-software-engineer"
    ),
    "generic_html": "https://careers.example/jobs/42?source=fixture",
    "greenhouse": "https://boards.greenhouse.io/example/jobs/1234",
    "lever": (
        "https://jobs.lever.co/example/"
        "123e4567-e89b-12d3-a456-426614174000"
    ),
    "recruitee": "https://example.recruitee.com/o/software-engineer",
    "smartrecruiters": (
        "https://jobs.smartrecruiters.com/example/"
        "743999123456789-Software-Engineer"
    ),
    "teamtailor": "https://acme.teamtailor.com/jobs/senior-engineer",
    "workable": "https://apply.workable.com/exampleco/j/abc123def",
    "workday": (
        "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers/job/"
        "San-Francisco/Software_Engineer_R12345"
    ),
}

PRIVATE_PROMPT = "PRIVATE_PROMPT_MJ_REQ_224"
PRIVATE_VALUE = "PRIVATE_VALUE_MJ_REQ_224"
CONTROLLED_FORM = (
    f'<form><label for="custom_q">{PRIVATE_PROMPT}</label>'
    f'<input id="custom_q" name="custom_q" type="text" required '
    f'value="{PRIVATE_VALUE}"></form>'
)


def test_registry_lists_every_adapter_with_a_controlled_contract_fixture():
    assert providers() == tuple(CANONICAL_ROUTES)
    assert all(get_provider(provider) is not None for provider in CANONICAL_ROUTES)


@pytest.mark.parametrize("provider, url", CANONICAL_ROUTES.items())
def test_routes_normalize_without_weakening_provider_identity(provider, url):
    normalized = normalize_route(provider, {"provider": provider, "url": url})

    assert normalized is not None
    assert normalized["provider"] == provider
    assert normalized["url"].startswith("https://")
    assert normalize_route(provider, {"provider": "wrong-provider", "url": url}) is None
    assert normalize_route(
        provider,
        {"provider": provider, "url": url.replace("https://", "http://", 1)},
    ) is None
    assert normalize_route(
        provider,
        {"provider": provider, "url": url.replace("https://", "https://user@", 1)},
    ) is None


@pytest.mark.parametrize("provider", CANONICAL_ROUTES)
def test_public_adapter_action_and_confirmation_boundaries_are_fail_closed(provider):
    declared = capabilities(provider)

    assert declared is not None
    assert declared["route_normalization"] is True
    assert declared["document_upload"] is False
    assert declared["live_form_interaction"] is False
    assert declared["submission"] is False
    assert declared.get("autofill", False) is False
    assert declared.get("confirmation", False) is False
    assert declared.get("mock_fixture_upload", False) in {True, False}
    assert (
        declared.get("human_review_for_uncertainty") is True
        or declared.get("human_review_for_unknown_fields") is True
    )


@pytest.mark.parametrize("provider, url", CANONICAL_ROUTES.items())
def test_controlled_form_analysis_is_value_free_and_side_effect_free(provider, url):
    with (
        patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
        patch.object(Popen, "__init__", side_effect=AssertionError("browser forbidden")),
    ):
        report = analyze_application_form(provider, CONTROLLED_FORM, route_url=url)

    assert report["status"] in {"ANALYZED", "REVIEW_REQUIRED"}
    assert isinstance(report.get("fields"), list)
    serialized = json.dumps(report, sort_keys=True)
    assert PRIVATE_PROMPT not in serialized
    assert PRIVATE_VALUE not in serialized
    assert "submission" not in report
    assert "confirmation" not in report
    for field in report["fields"]:
        assert set(field) == {"id", "name", "type", "required", "review_reason"}


def test_unknown_provider_and_cross_provider_route_fail_without_analysis():
    assert capabilities("unknown-provider") is None
    assert get_provider("unknown-provider") is None
    assert normalize_route(
        "lever",
        {"provider": "lever", "url": CANONICAL_ROUTES["greenhouse"]},
    ) is None
    assert analyze_application_form(
        "unknown-provider",
        CONTROLLED_FORM,
        route_url="https://careers.example/jobs/42",
    ) == {
        "status": "REVIEW_REQUIRED",
        "reason": "PROVIDER_UNSUPPORTED",
        "fields": [],
    }
