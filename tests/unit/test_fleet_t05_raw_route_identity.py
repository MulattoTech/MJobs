"""Raw public route identity must survive neither URL stripping nor casefolding."""
from unittest.mock import patch

import pytest

from services.applications.form_review_providers import normalize_application_route


ROUTES = (
    ("smartrecruiters", "https://jobs.smartrecruiters.com/steam/123456789012-special", "123456789012"),
    ("workable", "https://apply.workable.com/steam/j/abc12s", "abc12s"),
    ("teamtailor", "https://steam.teamtailor.com/jobs/123-special", "123-special"),
    ("bamboohr", "https://steam.bamboohr.com/careers/123", "123"),
    ("greenhouse", "https://boards.greenhouse.io/steam/jobs/123", "123"),
    ("lever", "https://jobs.lever.co/steam/123e4567-e89b-12d3-a456-426614174000", "123e4567-e89b-12d3-a456-426614174000"),
    ("ashby", "https://jobs.ashbyhq.com/steam/engineering-123", "engineering-123"),
)


@pytest.mark.parametrize("provider,url,requisition", ROUTES)
def test_ascii_identity_and_unicode_tracking_query_keep_adapter_contract(provider, url, requisition):
    metadata = {"tenant": "STEAM", "requisition": requisition.upper()}
    from services.applications.provider_registry import normalize_route
    raw_url = url.replace("steam", "STEAM") + "?utm_source=caf\u00e9"
    expected = normalize_route(provider, {"provider": provider, "url": raw_url, **metadata})
    assert expected is not None
    assert normalize_application_route(provider.upper(), raw_url, route_metadata=metadata) == expected


@pytest.mark.parametrize("provider,url,requisition", ROUTES)
@pytest.mark.parametrize("mutation", ["host", "tenant", "metadata", "space", "leading_c0", "embedded_c0"])
def test_raw_ambiguous_identity_is_rejected_before_adapter(provider, url, requisition, mutation):
    metadata = {"tenant": "steam", "requisition": requisition}
    raw_url = url
    if mutation == "host":
        authority_end = url.index("/", len("https://"))
        authority = url[len("https://"):authority_end]
        raw_authority = (authority.replace("s", "\u017f") if "s" in authority
                         else authority.replace("k", "\u212a"))
        raw_url = "https://" + raw_authority + url[authority_end:]
    elif mutation == "tenant":
        raw_url = url.replace("steam", "\u017fteam")
    elif mutation == "metadata":
        metadata["tenant"] = "\u017fteam"
    elif mutation == "space":
        raw_url = " " + url
    elif mutation == "leading_c0":
        raw_url = "\x00" + url
    else:
        raw_url = url.replace("steam", "\tsteam")
    with patch("services.applications.form_review_providers.normalize_route") as adapter_boundary:
        assert normalize_application_route(provider, raw_url, route_metadata=metadata) is None
    adapter_boundary.assert_not_called()


@pytest.mark.parametrize("provider,url,requisition", ROUTES)
def test_kelvin_tenant_metadata_cannot_casefold_to_ascii(provider, url, requisition):
    assert normalize_application_route(provider, url.replace("steam", "kteam"), route_metadata={
        "tenant": "\u212ateam", "requisition": requisition,
    }) is None


@pytest.mark.parametrize("provider,url,requisition", ROUTES)
def test_unicode_requisition_metadata_is_rejected_before_adapter(provider, url, requisition):
    with patch("services.applications.form_review_providers.normalize_route") as adapter_boundary:
        assert normalize_application_route(provider, url, route_metadata={
            "tenant": "steam", "requisition": "\u017f123",
        }) is None
    adapter_boundary.assert_not_called()
