"""Regression contracts for provider identity and uncertain static forms."""
import json

import pytest

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.form_review_providers import analyze_application_form


LEVER_JOB = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.mark.parametrize("adapter,url", [
    (GreenhouseApplicationAdapter(), "https://boards.greenhouse.io/longſ/jobs/123"),
    (GreenhouseApplicationAdapter(), "https://boards.greenhouse.io/K/jobs/123"),
    (LeverApplicationAdapter(), f"https://jobs.lever.co/longſ/{LEVER_JOB}"),
    (LeverApplicationAdapter(), f"https://jobs.lever.co/K/{LEVER_JOB}"),
])
def test_route_identity_does_not_alias_unicode_tenant(adapter, url):
    assert adapter.normalize_route({"provider": adapter.provider, "url": url}) is None


@pytest.mark.parametrize("adapter,url", [
    (GreenhouseApplicationAdapter(), "https://boards.greenhouse.io/example/jobs/123"),
    (LeverApplicationAdapter(), f"https://jobs.lever.co/example/{LEVER_JOB}"),
])
@pytest.mark.parametrize("mutation", [
    lambda url: " " + url,
    lambda url: url + " ",
    lambda url: url.replace("example", "exam\nple"),
    lambda url: url + "?source=one\ttwo",
    lambda url: url + "?source=one\\two",
])
def test_route_rejects_characters_urlsplit_would_discard(adapter, url, mutation):
    assert adapter.normalize_route({"provider": adapter.provider, "url": mutation(url)}) is None


@pytest.mark.parametrize("prompt", ["Email 世界", "世界 Email", "Email K"])
def test_lever_unknown_prompt_cannot_become_contact_by_dropping_unicode(prompt):
    report = analyze_application_form(
        "lever",
        f'<form><label for="email">{prompt}</label>'
        '<input id="email" name="email" type="email" value="PRIVATE_VALUE"></form>',
        route_url=f"https://jobs.lever.co/example/{LEVER_JOB}",
    )
    assert report["status"] == "REVIEW_REQUIRED"
    assert report["fields"][0]["review_reason"] == "UNKNOWN_QUESTION"
    assert "PRIVATE_VALUE" not in json.dumps(report)


@pytest.mark.parametrize("attribute", ["placeholder", "aria-label"])
def test_lever_unknown_attribute_prompt_cannot_be_overridden_by_contact_name(attribute):
    report = analyze_application_form(
        "lever", f'<form><input id="email" name="email" {attribute}="Email 世界"></form>',
        route_url=f"https://jobs.lever.co/example/{LEVER_JOB}",
    )
    assert report["status"] == "REVIEW_REQUIRED"
    assert report["fields"][0]["review_reason"] == "UNKNOWN_QUESTION"


@pytest.mark.parametrize("provider,url", [
    ("ashby", "https://jobs.ashbyhq.com/example/job"),
    ("lever", f"https://jobs.lever.co/example/{LEVER_JOB}"),
])
@pytest.mark.parametrize("markup", [
    '<form><input id="email" name="email" type="email">',
    '<form><input id="email" name="email" type="email"><label>unknown</form>',
    '<form><input id="email" name="email" type="email"><script>',
])
def test_interrupted_markup_never_becomes_analyzed(provider, url, markup):
    report = analyze_application_form(provider, markup, route_url=url)
    assert report == {"status": "REVIEW_REQUIRED", "reason": "MARKUP_PARSE_FAILED", "fields": []}


@pytest.mark.parametrize("provider,url", [
    ("ashby", "https://jobs.ashbyhq.com/example/job"),
    ("lever", f"https://jobs.lever.co/example/{LEVER_JOB}"),
])
def test_complete_form_stays_analyzed_and_value_free(provider, url):
    report = analyze_application_form(
        provider,
        '<form><label for="email">Email address</label>'
        '<input id="email" name="email" type="email" required value="PRIVATE_VALUE"></form>',
        route_url=url,
    )
    assert report == {
        "status": "ANALYZED", "reason": None,
        "fields": [{"id": "email", "name": "email", "type": "email",
                    "required": True, "review_reason": None}],
    }
