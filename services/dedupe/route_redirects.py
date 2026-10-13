"""Bounded, read-only resolution of aggregator routes to verified destinations."""
from __future__ import annotations

import ipaddress
import re
import time
import urllib.robotparser
from urllib.parse import urlsplit, urlunsplit

from services.company_discovery.public_client import (
    PublicCompanyClient,
    PublicFetchError,
    USER_AGENT,
    _origin,
    _parse_url,
)


ROUTE_CHECK_TIMEOUT_SECONDS = 8

# Only these exact public ATS hosts are recognized. Provider labels alone never
# authorize a destination; an exact verified tenant must also appear in its URL.
ATS_HOSTS = {
    "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
    "lever": {"jobs.lever.co", "jobs.eu.lever.co"},
    "ashby": {"jobs.ashbyhq.com"},
    "workable": {"apply.workable.com", "jobs.workable.com"},
    "smartrecruiters": {"jobs.smartrecruiters.com"},
    "jobvite": {"jobs.jobvite.com"},
    "icims": {"jobs.icims.com"},
    "workday": {"*.myworkdayjobs.com"},
}


def _domain(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 253:
        return None
    host = value.strip().casefold().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if (not host or host.startswith(".") or host.endswith(".")
            or any(not label or len(label) > 63 for label in host.split("."))
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in host.split("."))
            or host in {"localhost", "localhost.localdomain"}
            or host.endswith((".localhost", ".local", ".internal"))):
        return None
    return host


def _tenant_identifiers(row):
    if not isinstance(row, dict):
        return set()
    confidence = row.get("confidence", 0)
    evidence = row.get("evidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return set()
    if not 0.75 <= confidence <= 1.0 or not isinstance(evidence, list) or not any(
            isinstance(item, str) and item.strip() for item in evidence):
        return set()
    return {
        str(row.get(key) or "").strip().casefold()
        for key in ("tenant_identifier", "organization_identifier", "board_identifier")
        if str(row.get(key) or "").strip()
    }


def _ats_tenant_for_url(host, path, tenants):
    labels = set(host.split("."))
    if host.endswith(".myworkdayjobs.com"):
        labels.add(host[: -len(".myworkdayjobs.com")].split(".")[-1])
    path_parts = {part.casefold() for part in path.split("/") if part}
    observed = labels | path_parts
    for row in tenants:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").casefold()
        hosts = ATS_HOSTS.get(provider, set())
        host_matches = host in hosts or (
            "*.myworkdayjobs.com" in hosts and host.endswith(".myworkdayjobs.com")
        )
        identifiers = _tenant_identifiers(row)
        if provider == 'greenhouse':
            # Tenant/org/board metadata elsewhere in a URL is not its board
            # identity. Accept only the provider's structural public URL forms;
            # encoded separators and dot segments must not change that slot.
            match = re.fullmatch(r'/([A-Za-z0-9][A-Za-z0-9_-]{0,79})(?:/jobs/[0-9]+)?/?', path)
            tenant = str(row.get('tenant_identifier') or '')
            if (host_matches and identifiers and match
                    and match[1].casefold() == tenant.casefold()):
                return {'provider': provider, 'tenant': tenant}
            continue
        if provider == 'lever':
            from services.discovery.sources.lever import listing_url
            tenant = str(row.get('tenant_identifier') or '')
            try:
                matches = host_matches and identifiers and listing_url(tenant, f'https://{host}{path}')
            except ValueError:
                matches = False
            if matches:
                return {'provider': provider, 'tenant': tenant}
            continue
        if host_matches and identifiers.intersection(observed):
            return {"provider": provider, "tenant": str(row.get("tenant_identifier") or "")}
    return None


def _sanitized_candidate_url(url):
    parsed = urlsplit(str(url))
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))


def qualified_ats_tenant(url, tenants):
    """Bind current tenant evidence to the complete public destination URL."""
    parsed = _parse_url(url)
    if parsed is None:
        return None
    value, host, _ = parsed
    tenant = _ats_tenant_for_url(host, value.path, tenants)
    if tenant and tenant['provider'] == 'lever':
        from services.discovery.sources.lever import listing_url
        if not listing_url(tenant['tenant'], url):
            return None
    return tenant


class RouteRedirectProbeClient(PublicCompanyClient):
    """Reuse the pinned-public-IP transport with a narrow redirect destination policy."""

    def __init__(self, website, *, verified_domains, ats_tenants, check=None,
                 total_timeout=ROUTE_CHECK_TIMEOUT_SECONDS):
        super().__init__(website, check=check, total_timeout=total_timeout)
        self._verified_domains = tuple(
            domain for domain in (_domain(value) for value in verified_domains) if domain
        )
        self._ats_tenants = [row for row in ats_tenants if isinstance(row, dict)]
        self._redirect_robots = {}

    def allowed(self, url):
        parsed = _parse_url(url)
        if parsed is None:
            return False
        value, host, port = parsed
        if _origin(value, host, port) == self._origin:
            return True
        if any(host == domain or host.endswith("." + domain) for domain in self._verified_domains):
            return True
        return qualified_ats_tenant(url, self._ats_tenants) is not None

    def _robots_for(self, url, deadline):
        parsed = _parse_url(url)
        if parsed is None:
            raise PublicFetchError("URL_NOT_ALLOWED")
        value, host, port = parsed
        origin = _origin(value, host, port)
        if origin == self._origin:
            return super()._robots_for(url, deadline)
        if origin in self._redirect_robots:
            allowed, parser = self._redirect_robots[origin]
            if not allowed:
                raise PublicFetchError("ROBOTS_DENIED")
            return parser

        robots_host = f"[{host}]" if ":" in host else host
        robots_url = urlunsplit(("https", robots_host, "/robots.txt", "", ""))
        try:
            response = self._request_once(robots_url, deadline)
        except PublicFetchError:
            raise PublicFetchError("ROBOTS_UNAVAILABLE") from None
        if response.status_code == 404:
            self._redirect_robots[origin] = (True, None)
            return None
        if response.status_code in {401, 403, 429} or not 200 <= response.status_code < 300:
            self._redirect_robots[origin] = (False, None)
            raise PublicFetchError("ROBOTS_DENIED")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.content.decode("utf-8", errors="replace").splitlines())
        self._robots_delay = max(
            self._robots_delay,
            float(parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0.0),
        )
        rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
        if rate and rate.requests:
            self._robots_delay = max(self._robots_delay, float(rate.seconds) / max(1, rate.requests))
        self._redirect_robots[origin] = (True, parser)
        return parser


def resolve_aggregator_redirect(url, *, verified_domains, ats_tenants, check=None):
    """Follow a bounded public redirect chain and return only a verified destination candidate.

    Remote bodies and query strings are never returned or persisted. Failures expose
    stable transport codes only; this function never submits or changes a route.
    """
    try:
        client = RouteRedirectProbeClient(
            url,
            verified_domains=verified_domains,
            ats_tenants=ats_tenants,
            check=check,
        )
    except PublicFetchError as error:
        return {"status": "BLOCKED", "reason": error.code}
    try:
        response = client.get(url)
        final_url = str(response.url)
        parsed = _parse_url(final_url)
        if parsed is None:
            return {"status": "BLOCKED", "reason": "URL_NOT_ALLOWED"}
        final, host, _ = parsed
        if _origin(final, host, parsed[2]) == client._origin:
            return {"status": "UNRESOLVED"}
        employer = any(host == domain or host.endswith("." + domain)
                       for domain in client._verified_domains)
        if employer:
            return {"status": "RESOLVED_EMPLOYER_ROUTE",
                    "candidate_url": _sanitized_candidate_url(final_url)}
        tenant = qualified_ats_tenant(final_url, client._ats_tenants)
        if tenant:
            return {"status": "RESOLVED_ATS_ROUTE",
                    "candidate_url": _sanitized_candidate_url(final_url), **tenant}
        return {"status": "UNRESOLVED"}
    except PublicFetchError as error:
        return {"status": "BLOCKED", "reason": error.code}
    finally:
        client.close()
