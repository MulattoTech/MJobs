"""Contract oracle for every registered employer-specific ATS source."""
from __future__ import annotations

import unittest
from urllib.parse import urlsplit

import httpx

from services.company_discovery.ats_enrollment import enroll_public_ats
from services.company_discovery.public_client import PublicFetchError
from services.discovery.sources import registry
from services.discovery.sources.errors import SourceError


COMPANY_ATS_PROVIDERS = frozenset({"greenhouse", "lever"})
GLOBAL_SOURCE_PROVIDERS = frozenset({"remotive", "wwr", "remoteok"})


def _registered_provider_names() -> frozenset[str]:
    """Bind coverage to the immutable production registry's canonical IDs."""
    return frozenset(registry.DEFAULT_SOURCE_ADAPTER_REGISTRY.providers())


def _greenhouse_job(*, tenant: str = "acme", identifier: int = 101) -> dict:
    return {
        "id": identifier,
        "title": "Senior Python Engineer",
        "content": "<p>Remote Python role.</p>",
        "location": {"name": "Remote, United States"},
        "absolute_url": f"https://job-boards.greenhouse.io/{tenant}/jobs/{identifier}",
    }


def _lever_job(*, tenant: str = "acme", identifier: int = 101) -> dict:
    posting_id = f"00000000-0000-0000-0000-{identifier:012d}"
    return {
        "id": posting_id,
        "text": "Senior Python Engineer",
        "description": "<p>Remote Python role.</p>",
        "categories": {"location": "Remote, United States", "commitment": "Full-time"},
        "workplaceType": "remote",
        "hostedUrl": f"https://jobs.lever.co/{tenant}/{posting_id}",
        "applyUrl": f"https://jobs.lever.co/{tenant}/{posting_id}/apply",
    }


def _adapter(provider: str, *, invalid_response: httpx.Response | None = None):
    if provider == "greenhouse":
        policy = {"provider": provider, "tenant": "AcMe"}

        def respond(request: httpx.Request) -> httpx.Response:
            if invalid_response is not None:
                return invalid_response
            if request.url.path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": [_greenhouse_job()]})
            return httpx.Response(200, json={"name": "Acme Corporation"})

    elif provider == "lever":
        policy = {"provider": provider, "tenant": "GLOBAL:AcMe"}

        def respond(request: httpx.Request) -> httpx.Response:
            return invalid_response or httpx.Response(200, json=[_lever_job()])

    else:  # pragma: no cover - the exact registry assertion guards this helper
        raise AssertionError(provider)
    return registry.source_adapter(policy, transport=httpx.MockTransport(respond))


def _validated_inventory(adapter) -> tuple[tuple[str, str, str], ...]:
    """Validate the whole batch before returning anything eligible to persist."""
    batch = adapter.search()
    if not batch.complete_inventory or len(batch.items) > 1000:
        raise AssertionError("company ATS inventory must be complete and bounded")
    expected_identity = (adapter.__class__.__name__.casefold(), adapter.tenant)
    if batch.source_company is None or batch.source_company.identity_parts != expected_identity:
        raise AssertionError("company identity must be corroborated by provider and canonical tenant")
    identities = []
    for item in batch.items:
        if item.company != batch.source_company:
            raise AssertionError("a company ATS batch cannot cross employer identity")
        job = adapter.normalize(item.raw)
        url = urlsplit(job["application_url"])
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise AssertionError("application URL must be an absolute HTTP(S) URL")
        if job["attributes"]["provider"] != expected_identity[0]:
            raise AssertionError("normalized provider drifted from the registered adapter")
        if job["attributes"]["tenant"] != adapter.tenant:
            raise AssertionError("normalized tenant drifted from the canonical tenant")
        identities.append((expected_identity[0], adapter.tenant, job["requisition"]))
    if len(identities) != len(set(identities)):
        raise AssertionError("provider-scoped job identifiers must be unique")
    return tuple(identities)


class CompanySourceContractSustain224Tests(unittest.TestCase):
    def test_exact_registered_company_ats_coverage(self):
        registered = _registered_provider_names()
        self.assertEqual(registered, COMPANY_ATS_PROVIDERS | GLOBAL_SOURCE_PROVIDERS)
        self.assertEqual(
            {provider for provider in registered if provider not in GLOBAL_SOURCE_PROVIDERS},
            COMPANY_ATS_PROVIDERS,
        )

    def test_each_company_ats_has_canonical_identity_bounded_jobs_and_stable_urls(self):
        for provider in sorted(COMPANY_ATS_PROVIDERS):
            with self.subTest(provider=provider):
                first = _adapter(provider)
                second = _adapter(provider)
                expected_tenant = "acme" if provider == "greenhouse" else "global:acme"
                self.assertEqual(first.tenant, expected_tenant)
                self.assertEqual(_validated_inventory(first), _validated_inventory(second))

    def test_missing_malformed_and_redirected_source_data_fail_closed(self):
        for provider, missing_tenant in (("greenhouse", ""), ("lever", "global:")):
            with self.subTest(provider=provider, case="missing-tenant"):
                with self.assertRaises(ValueError):
                    registry.source_adapter({"provider": provider, "tenant": missing_tenant})

        for provider in sorted(COMPANY_ATS_PROVIDERS):
            with self.subTest(provider=provider, case="redirect"):
                redirected = httpx.Response(302, headers={"location": "https://attacker.invalid/jobs"})
                with self.assertRaisesRegex(SourceError, "SOURCE_REDIRECT_BLOCKED"):
                    _validated_inventory(_adapter(provider, invalid_response=redirected))

        malformed = {
            "greenhouse": {**_greenhouse_job(), "id": "101"},
            "lever": {**_lever_job(), "id": "not-a-provider-id"},
        }
        cross_tenant = {
            "greenhouse": _greenhouse_job(tenant="other-company"),
            "lever": _lever_job(tenant="other-company"),
        }
        for provider in sorted(COMPANY_ATS_PROVIDERS):
            adapter = _adapter(provider)
            for case, row in (("malformed", malformed[provider]),
                              ("cross-tenant", cross_tenant[provider])):
                with self.subTest(provider=provider, case=case):
                    with self.assertRaises(SourceError):
                        adapter.normalize(row)

    def test_greenhouse_application_url_contract_uses_exact_failure_code(self):
        adapter = _adapter("greenhouse")
        invalid_urls = {
            "http": "http://job-boards.greenhouse.io/acme/jobs/101",
            "attacker-host": "https://attacker.invalid/acme/jobs/101",
            "userinfo": "https://user@job-boards.greenhouse.io/acme/jobs/101",
            "disallowed-port": "https://job-boards.greenhouse.io:444/acme/jobs/101",
            "cross-tenant": "https://job-boards.greenhouse.io/other/jobs/101",
            "literal-traversal": "https://job-boards.greenhouse.io/acme/../other/jobs/101",
            "encoded-traversal": "https://job-boards.greenhouse.io/acme/%2e%2e/other/jobs/101",
        }
        for case, url in invalid_urls.items():
            with self.subTest(case=case), self.assertRaises(SourceError) as raised:
                adapter.normalize({**_greenhouse_job(), "absolute_url": url})
            self.assertEqual(raised.exception.code, "COMPANY_ATS_JOB_TENANT_MISMATCH")

        for host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
            with self.subTest(host=host):
                url = f"https://{host}/acme/jobs/101"
                normalized = adapter.normalize({**_greenhouse_job(), "absolute_url": url})
                self.assertEqual(normalized["application_url"], url)
                self.assertEqual(normalized["attributes"]["application_url_status"], "PROVIDER_SUPPLIED")

        for missing in (None, ""):
            with self.subTest(missing=missing):
                normalized = adapter.normalize({**_greenhouse_job(), "absolute_url": missing})
                self.assertEqual(normalized["application_url"], "")
                self.assertEqual(normalized["attributes"]["application_url_status"], "UNAVAILABLE")

    def test_greenhouse_unbound_policy_is_instance_local_and_direct_calls_stay_strict(self):
        unbound = _adapter("greenhouse")
        unbound.set_authoritative_tenant_binding(False)
        sanitized = unbound.normalize(_greenhouse_job(tenant="other-company"))
        self.assertEqual(sanitized["application_url"], "")
        self.assertEqual(sanitized["attributes"]["application_url_status"], "UNAVAILABLE")

        direct = _adapter("greenhouse")
        with self.assertRaises(SourceError) as raised:
            direct.normalize(_greenhouse_job(tenant="other-company"))
        self.assertEqual(raised.exception.code, "COMPANY_ATS_JOB_TENANT_MISMATCH")

    def test_ambiguous_official_page_tenants_fail_before_enrollment(self):
        cases = {
            "greenhouse": (
                '<a href="https://boards.greenhouse.io/acme">A</a>'
                '<a href="https://boards.greenhouse.io/other">B</a>'
            ),
            "lever": (
                '<a href="https://jobs.lever.co/acme">A</a>'
                '<a href="https://jobs.eu.lever.co/acme">B</a>'
            ),
        }
        for provider, html in cases.items():
            with self.subTest(provider=provider), self.assertRaisesRegex(
                PublicFetchError, "COMPANY_ATS_TENANT_REVIEW_REQUIRED"
            ):
                enroll_public_ats(
                    None,
                    {},
                    {},
                    "https://employer.example/careers",
                    html,
                    profile_id=None,
                    check=lambda: None,
                    provider=provider,
                )


if __name__ == "__main__":
    unittest.main()
