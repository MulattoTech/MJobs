"""Bounded public Arbeitnow discovery; no credentials or application submission.

MJ-REQ-017.F07. One latest API page is a partial observation, never an inventory.
The provider's listing URL supplies its company/posting identity; display names
and regional domains do not establish an employer identity or applicant country.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from services.discovery.negative_filters import parse_negative_conditions
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.search_semantics import (
    classify_location, normalize_employment_type, normalize_experience_level,
    normalize_title, parse_education_requirement, parse_salary,
)
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)
from services.discovery.sources.errors import SourceError
from services.discovery.sources.http_transport import SourceTransport
from services.discovery.sources.retry_after import retry_after_seconds
from services.discovery.sources.structured_text import structured_text

API_URL = "https://www.arbeitnow.com/api/job-board-api"
ATTRIBUTION_URL = "https://www.arbeitnow.com"
# Explicit regional sites linked by the provider's official home page.
LISTING_HOSTS = frozenset({
    "www.arbeitnow.com", "www.arbeitnow.co.uk", "www.arbeitnow.fr",
    "www.arbeitnow.ch", "www.arbeitnow.nl",
})
MAX_RESPONSE_BYTES = 5_000_000
MAX_PAGE_ITEMS = 1000
POLL_INTERVAL = 0.05
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _label(value, *, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise SourceError("SOURCE_INVALID_JOB")
    value = " ".join(structured_text(value).split())
    if not value:
        raise SourceError("SOURCE_INVALID_JOB")
    return value


def _listing_identity(raw):
    """Require the provider-native path, preserving the original safe URL."""
    if not isinstance(raw, dict):
        raise SourceError("SOURCE_INVALID_JOB")
    slug = raw.get("slug")
    url = raw.get("url")
    if (not isinstance(slug, str) or len(slug) > 512 or not _SLUG.fullmatch(slug)
            or not isinstance(url, str) or len(url) > 2048
            or "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
        raise SourceError("SOURCE_INVALID_JOB")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in LISTING_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query or parsed.fragment):
            raise ValueError()
        parts = parsed.path.split("/")
        if len(parts) == 6 and parts[-1] == "":
            parts.pop()
        if (len(parts) != 5 or parts[:3] != ["", "jobs", "companies"]
                or not parts[3] or len(parts[3]) > 256
                or not _SLUG.fullmatch(parts[3]) or parts[4] != slug):
            raise ValueError()
    except ValueError:
        raise SourceError("SOURCE_INVALID_JOB") from None
    return parsed.hostname, parts[3], slug, url


def company_identity(raw):
    """Return an unverified provider-local identity, never a name-derived guess."""
    host, company_slug, _slug, _url = _listing_identity(raw)
    name = _label(raw.get("company_name"), limit=1000)
    return SourceCompany(
        identity_parts=("arbeitnow", host, company_slug),
        name=name,
        domain="https://" + host,
    )


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _strings(value):
    if not isinstance(value, list):
        return []
    return [" ".join(structured_text(item).split())[:200]
            for item in value[:100]
            if isinstance(item, str) and item.strip()]


def _invalid_json_constant(_value):
    raise ValueError()


def _finite_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError()
    return parsed


class Arbeitnow:
    def __init__(self, tenant="default", transport=None, check=None,
                 request_timeout=10, rate_limit=60, concurrency=1):
        if not isinstance(tenant, str) or tenant.strip().casefold() != "default":
            raise ValueError("SOURCE_INVALID_TENANT")
        if type(request_timeout) is not int or not 2 <= request_timeout <= 60:
            raise ValueError("SOURCE_INVALID_REQUEST_TIMEOUT")
        if type(rate_limit) is not int or not 1 <= rate_limit <= 600:
            raise ValueError("SOURCE_INVALID_RATE_LIMIT")
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("SOURCE_INVALID_CONCURRENCY")
        self.tenant = "default"
        self.transport = transport
        self.check = check or (lambda: None)
        self.request_timeout = request_timeout
        self.rate_limit = rate_limit
        self.concurrency = concurrency
        self._request_slots = threading.BoundedSemaphore(concurrency)
        self._rate_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._last_request = None
        self.status = "NOT_CHECKED"

    def capabilities(self):
        return SourceCapabilities(CollectionStrategy.PUBLIC_JSON, frozenset({
            SourceCapability.DISCOVERY, SourceCapability.REMOTE_METADATA,
            SourceCapability.APPLICATION_REDIRECT,
        }))

    def health(self):
        with self._status_lock:
            return self.status

    def _set_status(self, status):
        with self._status_lock:
            self.status = status

    def _wait_for_rate_limit(self):
        interval = 60.0 / self.rate_limit
        with self._rate_lock:
            current = time.monotonic()
            scheduled = (current if self._last_request is None else
                         max(current, self._last_request + interval))
            self._last_request = scheduled
        while True:
            self.check()
            delay = scheduled - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, POLL_INTERVAL))

    def _request(self):
        self.check()
        while not self._request_slots.acquire(timeout=POLL_INTERVAL):
            self.check()
        try:
            # Injected fixture transports never wait for a real provider quota.
            if self.transport is None:
                self._wait_for_rate_limit()
            budget = float(self.request_timeout)
            remaining = getattr(self.check, "remaining_seconds", None)
            if callable(remaining):
                budget = min(budget, remaining())
            deadline = time.monotonic() + budget

            def bounded_check():
                self.check()
                if time.monotonic() >= deadline:
                    raise SourceError("SOURCE_TIME_LIMIT")

            bounded_check()
            transport = self.transport if self.transport is not None else SourceTransport(bounded_check)
            with httpx.Client(transport=transport, timeout=budget, follow_redirects=False,
                              trust_env=False, headers={
                                  "User-Agent": "MJobs-Discovery/1.0",
                                  "Accept": "application/json",
                              }) as client:
                bounded_check()
                with client.stream("GET", API_URL) as response:
                    bounded_check()
                    if response.is_redirect:
                        raise SourceError("SOURCE_REDIRECT_BLOCKED")
                    if response.status_code == 429:
                        raise SourceError("SOURCE_RATE_LIMITED", retry_seconds=retry_after_seconds(
                            response.headers.get("retry-after")))
                    if response.status_code >= 500:
                        raise SourceError("SOURCE_SERVER_ERROR", retry_seconds=retry_after_seconds(
                            response.headers.get("retry-after"), default=300, minimum=300))
                    if response.status_code != 200:
                        raise SourceError("SOURCE_UNAVAILABLE", retry_seconds=300)
                    media_type = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
                    if media_type != "application/json":
                        raise SourceError("SOURCE_PAYLOAD_INVALID")
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=None):
                        bounded_check()
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise SourceError("SOURCE_PAYLOAD_LIMIT")
                        content.extend(chunk)
                    bounded_check()
                    try:
                        payload = json.loads(content, parse_constant=_invalid_json_constant,
                                             parse_float=_finite_json_float)
                    except (ValueError, UnicodeError, RecursionError):
                        raise SourceError("SOURCE_PAYLOAD_INVALID") from None
                    bounded_check()
                    return payload
        except SourceError as failure:
            self._set_status(failure.code)
            raise
        except httpx.TimeoutException:
            self.check()
            self._set_status("SOURCE_TIMEOUT")
            raise SourceError("SOURCE_TIMEOUT", retry_seconds=120) from None
        except httpx.HTTPError:
            self.check()
            self._set_status("SOURCE_NETWORK_ERROR")
            raise SourceError("SOURCE_NETWORK_ERROR", retry_seconds=120) from None
        finally:
            self._request_slots.release()

    def search(self):
        """Observe only the latest page; never follow response-selected links."""
        payload = self._request()
        try:
            if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                    or len(payload["data"]) > MAX_PAGE_ITEMS):
                raise SourceError("SOURCE_PAYLOAD_INVALID")
            items = []
            for raw in payload["data"]:
                self.check()
                items.append(SourceItem(raw, company_identity(raw)))
            self.check()
        except SourceError as failure:
            # Required identity defects fail the batch instead of silently
            # discarding rows or guessing an employer from its display name.
            self._set_status(failure.code)
            raise
        self._set_status("HEALTHY")
        return SearchBatch(endpoint=API_URL, items=tuple(items), source_company=None,
                           complete_inventory=False)

    def fetch(self, job_id):
        """There is no supported individual-job API in this adapter."""
        raise SourceError("SOURCE_UNSUPPORTED")

    def normalize(self, raw):
        company = company_identity(raw)
        _host, _company_slug, slug, url = _listing_identity(raw)
        title = _label(raw.get("title"), limit=5000)[:500]
        if not isinstance(raw.get("description", ""), str):
            raise SourceError("SOURCE_INVALID_JOB")
        description = structured_text(raw.get("description", ""))[:50000]
        location = (structured_text(raw.get("location", ""))[:1000]
                    if isinstance(raw.get("location", ""), str) else "") or "Unknown"
        remote = raw.get("remote")
        # Only the source's location field supplies geography. Incidental "us"
        # or "global" in a description must not become US/worldwide eligibility.
        facts = classify_location(location, "Remote" if remote is True else "")
        remote_type = "REMOTE" if remote is True else (
            facts["remote_type"] if facts["remote_type"] in {"HYBRID", "ONSITE"} else "UNKNOWN")
        tags = _strings(raw.get("tags"))
        job_types = _strings(raw.get("job_types"))
        combined = f"{title} {description} {' '.join(tags)}"
        employment_text = " ".join(job_types).replace("fulltime", "full time").replace("parttime", "part time")
        employment = normalize_employment_type(employment_text)
        experience = normalize_experience_level(title)
        title_info = normalize_title(title)
        education = parse_education_requirement(description)
        skills = extract_skills(combined)
        requirements = parse_job_requirements(description)
        salary = parse_salary("")
        return {
            "requisition": slug,
            "title": title,
            "description": description,
            "skills": skills["labels"],
            "location": location,
            "remote_type": remote_type,
            "application_url": url,
            "attributes": {
                "provider": "arbeitnow",
                "tenant": self.tenant,
                "company_name": company.name[:200],
                "native_posting_id": slug,
                "native_job_id": slug,
                "source_attribution": {"name": "Arbeitnow", "url": ATTRIBUTION_URL},
                "provider_listing_url": url,
                "lifecycle_claim": "POSSIBLY_ACTIVE",
                "tags": tags,
                "job_types": job_types,
                "employment_type": employment,
                "experience_level": experience["level"],
                "experience_level_raw": experience["raw"],
                "normalized_title": title_info["normalized_title"],
                "title_family": title_info["title_family"],
                "skill_ids": skills["ids"],
                "skill_mentions": skills["mentions"],
                "education_requirement": education["classification"],
                "education_requirement_raw": education["raw"],
                "experience_requirements": requirements["experience"],
                "certifications": requirements["certifications"],
                "clearance": requirements["clearance"],
                "work_authorization": requirements["work_authorization"],
                "negative_conditions": parse_negative_conditions(description, requirements["clearance"]),
                "salary_min": salary["salary_min"],
                "salary_max": salary["salary_max"],
                "salary_period": salary["salary_period"],
                "salary_currency": salary["currency"],
                "salary_source": salary["salary_source"],
                "salary_raw": salary["salary_raw"],
                "reported_posted_at": _timestamp(raw.get("created_at")),
                "reported_posted_at_raw": raw.get("created_at"),
                "remote_regions": facts["remote_regions"] if remote is True else [],
                "remote_indicated": remote if type(remote) is bool else None,
                "country": facts["country"],
                "state": facts["state"],
                "city": facts["city"],
                "skill_evidence": "description_mentions",
                "submission_capability": "MANUAL_ONLY",
                "fictional": False,
                "application_url_status": "PROVIDER_SUPPLIED",
            },
        }
