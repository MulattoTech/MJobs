"""Documented public GET adapter. No credentials or application submission."""
from html import unescape
import math
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx

from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)

HOST = "https://boards-api.greenhouse.io"
SKILLS = ("Python", "SQL", "JavaScript", "TypeScript", "Java", "C#", "C++", "Go", "Rust", "Linux", "Windows", "Docker", "Kubernetes", "AWS", "Azure", "Terraform", "PowerShell", "React", "PostgreSQL")


class SourceError(ValueError):
    def __init__(self, code, retry_seconds=60):
        super().__init__(code)
        self.code = code
        self.retry_seconds = retry_seconds


def tenant_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("SOURCE_INVALID_TENANT")
    return value.casefold()


def plain_text(value):
    value = str(value or "")[:500_000]
    for _ in range(2):
        value = unescape(value)
    parsed = BeautifulSoup(value, "html.parser")
    for element in parsed(["script", "style", "noscript"]):
        element.decompose()
    return parsed.get_text(" ", strip=True)


class Greenhouse:
    def __init__(self, tenant, transport=None, check=None, request_timeout=10, rate_limit=60, concurrency=1):
        self.tenant = tenant_identifier(tenant)
        if type(request_timeout) is not int or not 2 <= request_timeout <= 60:
            raise ValueError("SOURCE_INVALID_REQUEST_TIMEOUT")
        if type(rate_limit) is not int or not 1 <= rate_limit <= 600:
            raise ValueError("SOURCE_INVALID_RATE_LIMIT")
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("SOURCE_INVALID_CONCURRENCY")
        self.transport = transport
        self.check = check or (lambda: None)
        self.request_timeout = request_timeout
        self.rate_limit = rate_limit
        self.concurrency = concurrency
        self._last_request = 0.0
        self.status = "NOT_CHECKED"

    def capabilities(self):
        return SourceCapabilities(CollectionStrategy.API, frozenset({
            SourceCapability.DISCOVERY, SourceCapability.JOB_DETAIL,
            SourceCapability.SALARY, SourceCapability.REMOTE_METADATA,
            SourceCapability.APPLICATION_REDIRECT,
        }))

    def health(self):
        return self.status

    def _get(self, suffix, params=None):
        try:
            return self._request(suffix, params)
        except SourceError as failure:
            self.status = failure.code
            raise
        except httpx.HTTPError:
            self.status = "SOURCE_NETWORK_ERROR"
            raise

    def _request(self, suffix, params=None):
        self.check()
        if self.transport is None and self._last_request:
            wait = (60 / self.rate_limit) - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()
        path = "/v1/boards/" + self.tenant + suffix
        with httpx.Client(timeout=self.request_timeout, follow_redirects=False, trust_env=False, transport=self.transport,
                          headers={"User-Agent": "MJobs/0.1 public-read-only", "Accept": "application/json"}) as client:
            with client.stream("GET", HOST + path, params=params) as response:
                if response.is_redirect:
                    raise SourceError("SOURCE_REDIRECT_BLOCKED")
                if response.status_code == 429:
                    try:
                        seconds = int(response.headers.get("retry-after", "60"))
                    except ValueError:
                        seconds = 86400
                    raise SourceError("SOURCE_RATE_LIMITED", max(60, seconds))
                if response.status_code != 200:
                    raise SourceError("SOURCE_HTTP_" + str(response.status_code))
                if "application/json" not in response.headers.get("content-type", "").lower():
                    raise SourceError("SOURCE_INVALID_CONTENT_TYPE")
                content = bytearray()
                deadline = time.monotonic() + max(5, min(120, self.request_timeout * 2))
                for chunk in response.iter_bytes(chunk_size=65536):
                    self.check()
                    content.extend(chunk)
                    if len(content) > 5_000_000:
                        raise SourceError("SOURCE_PAYLOAD_LIMIT")
                    if time.monotonic() > deadline:
                        raise SourceError("SOURCE_TIME_LIMIT")
                try:
                    payload = __import__("json").loads(content)
                except ValueError as error:
                    raise SourceError("SOURCE_INVALID_JSON") from error
                self.status = "HEALTHY"
                return payload

    def search(self):
        board = self._get("")
        payload = self._get("/jobs", {"content": "true"})
        if not isinstance(board, dict) or not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise SourceError("SOURCE_INVALID_FORMAT")
        if len(payload["jobs"]) > 1000:
            raise SourceError("SOURCE_JOB_LIMIT")
        company = SourceCompany(("greenhouse", self.tenant), plain_text(board.get("name") or self.tenant)[:200],
                                "https://job-boards.greenhouse.io/" + self.tenant)
        return SearchBatch(HOST + "/v1/boards/" + self.tenant + "/jobs",
                           tuple(SourceItem(raw, company) for raw in payload["jobs"]), company)

    def collect(self):
        """Compatibility for callers of the original bounded board collection API."""
        batch = self.search()
        return {"company": batch.source_company.name if batch.source_company else self.tenant,
                "jobs": [item.raw for item in batch.items], "endpoint": batch.endpoint}

    def fetch(self, job_id):
        if type(job_id) is not int or job_id <= 0:
            raise ValueError("SOURCE_INVALID_JOB_ID")
        raw = self._get("/jobs/" + str(job_id), {"pay_transparency": "true"})
        self.normalize(raw)
        if raw["id"] != job_id:
            raise SourceError("SOURCE_JOB_ID_MISMATCH")
        return raw

    def normalize(self, raw):
        if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0 or not isinstance(raw.get("title"), str) or not raw["title"].strip():
            raise SourceError("SOURCE_INVALID_JOB")
        description = plain_text(raw.get("content"))[:50000]
        if raw.get("location") and not isinstance(raw["location"], dict):
            raise SourceError("SOURCE_INVALID_JOB_LOCATION")
        location = plain_text((raw.get("location") or {}).get("name") or "Unknown")[:1000]
        remote = "HYBRID" if "hybrid" in location.casefold() else ("REMOTE" if "remote" in location.casefold() else "UNKNOWN")
        combined = plain_text(raw["title"]) + " " + description
        skills = [skill for skill in SKILLS if re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", combined, re.I)]
        url = str(raw.get("absolute_url") or "")
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in ("boards.greenhouse.io", "job-boards.greenhouse.io") or parsed.username or parsed.password or parsed.port not in (None, 443):
                url = ""
        except ValueError:
            url = ""
        ranges = []
        for row in (raw.get("pay_input_ranges") if isinstance(raw.get("pay_input_ranges"), list) else []):
            if not isinstance(row, dict):
                continue
            lower, upper = row.get("min_cents"), row.get("max_cents")
            if all(type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1e11 for value in (lower, upper)) and lower <= upper:
                ranges.append({"min": lower / 100, "max": upper / 100, "currency": str(row.get("currency_type") or "Unknown")[:8], "period": "UNKNOWN"})
        return {"requisition": str(raw["id"]), "title": plain_text(raw["title"])[:500], "description": description,
                "skills": skills, "location": location, "remote_type": remote, "application_url": url,
                "attributes": {"provider": "greenhouse", "tenant": self.tenant, "native_job_id": raw.get("internal_job_id"),
                    "native_posting_id": raw["id"], "updated_at": raw.get("updated_at"), "salary": ranges or None,
                    "skill_evidence": "description_mentions", "submission_capability": "MANUAL_ONLY", "fictional": False,
                    "application_url_status": "PROVIDER_SUPPLIED" if url else "UNAVAILABLE"}}
