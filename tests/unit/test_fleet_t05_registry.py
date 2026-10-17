"""The shared registry keeps provider routing and advertised behavior aligned."""
import pytest

from services.applications.provider_registry import (
    capabilities,
    get_provider,
    normalize_route,
    providers,
)


ROUTES = (
    ("ashby", "https://jobs.ashbyhq.com/fixture/engineering-123",
     {"tenant": "fixture", "requisition": "engineering-123"}),
    ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123", {}),
    ("lever", "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000", {}),
    ("recruitee", "https://example.recruitee.com/o/software-engineer", {}),
    ("workday", "https://fixture.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R123",
     {"tenant": "fixture", "requisition": "R123"}),
    ("smartrecruiters", "https://jobs.smartrecruiters.com/acme/123456789012-job", {}),
    ("workable", "https://apply.workable.com/acme/j/ABC123/", {}),
    ("teamtailor", "https://acme.teamtailor.com/jobs/123-job", {}),
    ("bamboohr", "https://acme.bamboohr.com/careers/123", {}),
    ("generic_html", "https://careers.example.test/job/123", {}),
    ("breezy", "https://acme.breezy.hr/p/389eb1b8c3ef-job",
     {"tenant": "acme", "requisition": "389eb1b8c3ef"}),
)
EXPECTED_PROVIDERS = tuple(provider for provider, _, _ in ROUTES)


def test_registry_exposes_the_complete_current_provider_set():
    assert set(providers()) == set(EXPECTED_PROVIDERS)


@pytest.mark.parametrize("provider,url,metadata", ROUTES)
def test_registry_dispatches_only_matching_provider_route(provider, url, metadata):
    adapter = get_provider(provider)
    assert adapter is not None
    normalized = normalize_route(provider, {
        "provider": provider, "url": url, **metadata,
    })
    assert normalized is not None
    assert normalized["provider"] == provider
    expected_url = url.rstrip("/") if provider == "workable" else url
    if provider == "workable":
        expected_url = expected_url.replace("ABC123", "abc123")
    assert normalized["url"] == expected_url

    assert normalize_route("greenhouse", {"provider": "lever", "url": url}) is None


@pytest.mark.parametrize("provider,url,metadata", ROUTES)
def test_registry_capabilities_do_not_claim_submission_or_real_upload(provider, url, metadata):
    declared = capabilities(provider)
    assert declared == get_provider(provider).capabilities()
    assert declared["route_normalization"] is True
    assert declared.get("document_upload") is False
    assert declared.get("submission") is False
    assert declared.get("live_form_interaction") is False


@pytest.mark.parametrize("provider,expected", [
    ("greenhouse", True),
    ("lever", True),
    ("recruitee", True),
    ("smartrecruiters", True),
    ("workable", True),
    ("teamtailor", True),
    ("workday", True),
    ("breezy", True),
    ("bamboohr", False),
    ("generic_html", False),
])
def test_registry_distinguishes_form_metadata_extraction_from_route_support(provider, expected):
    assert capabilities(provider)["form_metadata_extraction"] is expected


def test_registry_fails_closed_for_unknown_or_cross_provider_routes():
    assert get_provider("not-a-provider") is None
    assert normalize_route("smartrecruiters", {
        "provider": "workable",
        "url": "https://apply.workable.com/acme/j/ABC123/",
    }) is None

