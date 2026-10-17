"""Public dispatch must retain the stored route's provider identity."""
import pytest

from services.applications.form_review_providers import (
    analyze_application_form,
    normalize_application_route,
)


ROUTE = "https://boards.greenhouse.io/acme/jobs/123"


@pytest.mark.parametrize("metadata", [
    {"provider": "lever"},
    {"url": "https://boards.greenhouse.io/acme/jobs/456"},
    {"tenant": "other"},
    {"requisition": "456"},
    {"provider": None},
    {"tenant": None},
    {"requisition": None},
    [],
])
def test_metadata_cannot_change_or_erase_route_identity(metadata):
    assert normalize_application_route("greenhouse", ROUTE, route_metadata=metadata) is None


def test_provider_casefold_does_not_accept_unicode_aliases():
    route = "https://jobs.smartrecruiters.com/acme/123456789012-job"
    assert normalize_application_route("smartrecruiterſ", route) is None
    assert analyze_application_form("smartrecruiterſ", "", route_url=route)["reason"] == "PROVIDER_UNSUPPORTED"


@pytest.mark.parametrize("provider,url,reason", [
    ("smartrecruiters", "https://jobs.smartrecruiters.com/acme/123456789012-job",
     "DOM_INSPECTION_REQUIRED"),
    ("workable", "https://apply.workable.com/acme/j/ABC123/", "DOM_INSPECTION_REQUIRED"),
    ("teamtailor", "https://acme.teamtailor.com/jobs/123-job", "DOM_INSPECTION_REQUIRED"),
    ("bamboohr", "https://acme.bamboohr.com/careers/123", "FORM_SEMANTICS_UNSUPPORTED"),
    ("generic_html", "https://careers.example.test/job/123", "FORM_SEMANTICS_UNSUPPORTED"),
])
def test_merged_routes_are_reachable_without_fabricating_html_support(provider, url, reason):
    route = normalize_application_route(provider, url)
    assert route is not None
    assert route["provider"] == provider
    assert analyze_application_form(provider, b"PRIVATE_MARKUP", route_url=url) == {
        "status": "REVIEW_REQUIRED", "reason": reason, "fields": [],
    }


