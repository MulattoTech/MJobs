import unittest
from unittest.mock import patch

import httpx

from services.company_discovery.public_client import PublicFetchError
from services.dedupe.route_redirects import RouteRedirectProbeClient, resolve_aggregator_redirect


VERIFIED_TENANTS = [{
    "provider": "greenhouse",
    "tenant_identifier": "acme",
    "organization_identifier": "acme-org",
    "board_identifier": "acme-board",
    "confidence": 0.94,
    "evidence": ["PUBLIC_API_BOARD"],
}]


class DirectEmployerRouteRedirectTests(unittest.TestCase):
    def test_probe_only_allows_the_source_and_verified_employer_or_ats_destinations(self):
        client = RouteRedirectProbeClient(
            "https://jobs.example/redirect/42",
            verified_domains=["acme.example"],
            ats_tenants=VERIFIED_TENANTS,
        )
        self.assertTrue(client.allowed("https://jobs.example/redirect/42"))
        self.assertTrue(client.allowed("https://careers.acme.example/jobs/42"))
        self.assertTrue(client.allowed("https://boards.greenhouse.io/acme/jobs/42"))
        self.assertFalse(client.allowed("https://boards.greenhouse.io/other/jobs/42"))
        self.assertFalse(client.allowed("https://evil.example/jobs/42"))
        self.assertFalse(client.allowed("http://careers.acme.example/jobs/42"))
        self.assertFalse(client.allowed("https://127.0.0.1/jobs/42"))
        client.close()
        invalid_tenant = RouteRedirectProbeClient(
            "https://jobs.example/redirect/42", verified_domains=[],
            ats_tenants=[{**VERIFIED_TENANTS[0], "confidence": 1.01}],
        )
        self.assertFalse(invalid_tenant.allowed("https://boards.greenhouse.io/acme/jobs/42"))
        invalid_tenant.close()

    def test_redirect_to_verified_ats_returns_a_sanitized_candidate_without_persisting_it(self):
        route_url = "https://jobs.example/redirect/42?private=token"
        destination = "https://boards.greenhouse.io/acme/jobs/42?gh_src=private"
        def response(status, url, location=None):
            headers = {"location": location} if location else {}
            return httpx.Response(status, headers=headers, request=httpx.Request("GET", url))

        def request_once(client, url, deadline):
            return response(302, url, destination) if url == route_url else response(200, url)

        with patch.object(RouteRedirectProbeClient, "_load_robots"), \
             patch.object(RouteRedirectProbeClient, "_request_once", request_once), \
             patch("services.company_discovery.public_client.MIN_REQUEST_INTERVAL", 0):
            result = resolve_aggregator_redirect(
                route_url, verified_domains=["acme.example"], ats_tenants=VERIFIED_TENANTS
            )

        self.assertEqual(result, {
            "status": "RESOLVED_ATS_ROUTE",
            "candidate_url": "https://boards.greenhouse.io/acme/jobs/42",
            "provider": "greenhouse",
            "tenant": "acme",
        })

    def test_redirect_to_unverified_public_destination_fails_closed(self):
        route_url = "https://jobs.example/redirect/42"
        destination = "https://unverified.example/collect"
        def request_once(client, url, deadline):
            return httpx.Response(302, headers={"location": destination},
                                  request=httpx.Request("GET", url))

        with patch.object(RouteRedirectProbeClient, "_load_robots"), \
             patch.object(RouteRedirectProbeClient, "_request_once", request_once), \
             patch("services.company_discovery.public_client.MIN_REQUEST_INTERVAL", 0):
            result = resolve_aggregator_redirect(
                route_url, verified_domains=["acme.example"], ats_tenants=VERIFIED_TENANTS
            )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotIn("candidate_url", result)

    def test_route_without_a_redirect_is_unresolved(self):
        route_url = "https://jobs.example/listing/42"

        def request_once(client, url, deadline):
            return httpx.Response(200, request=httpx.Request("GET", url))

        with patch.object(RouteRedirectProbeClient, "_load_robots"), \
             patch.object(RouteRedirectProbeClient, "_request_once", request_once), \
             patch("services.company_discovery.public_client.MIN_REQUEST_INTERVAL", 0):
            result = resolve_aggregator_redirect(
                route_url, verified_domains=["acme.example"], ats_tenants=VERIFIED_TENANTS
            )
        self.assertEqual(result, {"status": "UNRESOLVED"})

    def test_verified_destination_robots_denial_blocks_the_route_check(self):
        client = RouteRedirectProbeClient(
            "https://jobs.example/redirect/42",
            verified_domains=[], ats_tenants=VERIFIED_TENANTS,
        )
        target = "https://boards.greenhouse.io/acme/jobs/42"
        denied = httpx.Response(403, request=httpx.Request("GET", "https://boards.greenhouse.io/robots.txt"))
        with patch.object(client, "_request_once", return_value=denied):
            with self.assertRaisesRegex(PublicFetchError, "ROBOTS_DENIED"):
                client._robots_for(target, client._deadline)
        client.close()


if __name__ == "__main__":
    unittest.main()
