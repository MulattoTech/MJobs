"""Bounded reader for a publicly published TestedRecruits company listing.

This adapter reads only the configured company-scoped embed listing. It does
not assert control of the company's domain or expose application capabilities.
"""
from __future__ import annotations

from html import unescape
import re
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx

from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)
from services.discovery.sources.errors import SourceError
from services.discovery.sources.structured_text import structured_text
from services.discovery.search_semantics import (
    normalize_employment_type, normalize_experience_level, normalize_title,
    parse_education_requirement, parse_salary,
)
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.negative_filters import parse_negative_conditions


HOST = "https://app.testedrecruits.com"
MAX_BODY_BYTES = 2_000_000
MAX_LISTINGS = 1_000
BODY_BUDGET_MAX_SECONDS = 120
CANCELLATION_POLL_INTERVAL = 0.05
_TENANT = re.compile(r"([1-9][0-9]{0,8})/([a-z0-9]+(?:-[a-z0-9]+){0,79})\Z")
_POSTING_PATH = re.compile(r"/posting/([1-9][0-9]{0,15})\Z")
_COUNT = re.compile(r"\bCurrent\s+Job\s+Postings\s*:\s*([0-9]+(?:,[0-9]{3})*)\b", re.I)


def _plain_text(value: str, limit: int) -> str:
    value = str(value or "")[:MAX_BODY_BYTES]
    for _ in range(2):
        value = unescape(value)
    parsed = BeautifulSoup(value, "html.parser")
    for element in parsed(("script", "style", "noscript", "template")):
        element.decompose()
    return parsed.get_text(" ", strip=True)[:limit]


class TestedRecruits:
    __test__ = False

    def __init__(self, tenant, transport=None, check=None, request_timeout=10, rate_limit=60, concurrency=1):
        if not isinstance(tenant, str) or len(tenant) > 87 or not _TENANT.fullmatch(tenant):
            raise ValueError("SOURCE_INVALID_TENANT")
        if type(request_timeout) is not int or not 2 <= request_timeout <= 60:
            raise ValueError("SOURCE_INVALID_REQUEST_TIMEOUT")
        if type(rate_limit) is not int or not 1 <= rate_limit <= 600:
            raise ValueError("SOURCE_INVALID_RATE_LIMIT")
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("SOURCE_INVALID_CONCURRENCY")
        self.company_number, self.slug = tenant.split("/", 1)
        self.tenant = f"{self.company_number}/{self.slug}"
        self.transport = transport
        self.check = check or (lambda: None)
        self.request_timeout = request_timeout
        self.rate_limit = rate_limit
        self.concurrency = concurrency
        self._status_lock = threading.Lock()
        self.status = "NOT_CHECKED"

    def capabilities(self):
        return SourceCapabilities(CollectionStrategy.PUBLIC_HTML, frozenset({
            SourceCapability.DISCOVERY,
        }))

    def health(self):
        with self._status_lock:
            return self.status

    @property
    def endpoint(self):
        return f"{HOST}/embedJobs/{self.company_number}/{self.slug}"

    def _get_listing(self) -> bytes:
        self.check()
        if self.transport is None:
            # PublicCompanyClient is the existing robots, DNS/SSRF, rate,
            # redirect, deadline, and bounded-streaming gate for public HTML.
            from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
            policy = SimpleNamespace(
                request_timeout=self.request_timeout,
                rate_limit=self.rate_limit,
                concurrency=self.concurrency,
            )
            client = PublicCompanyClient(
                self.endpoint, check=self.check,
                total_timeout=min(90, max(2, self.request_timeout * 2)),
                policy=policy,
            )
            try:
                response = client.get(self.endpoint)
            except PublicFetchError as error:
                raise SourceError(error.code) from None
            finally:
                client.close()
            if response.history:
                raise SourceError("SOURCE_REDIRECT_BLOCKED")
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise SourceError("SOURCE_INVALID_CONTENT_TYPE")
            content = response.content
            if len(content) > MAX_BODY_BYTES:
                raise SourceError("SOURCE_PAYLOAD_LIMIT")
            if not content.rstrip().lower().endswith(b"</html>"):
                raise SourceError("SOURCE_TRUNCATED")
            self.check()
            return content

        # Injected transports are for deterministic tests only. Production
        # reads go through PublicCompanyClient above so robots remains enforced.
        remaining_budget = getattr(self.check, "remaining_seconds", None)
        request_timeout = float(self.request_timeout)
        if callable(remaining_budget):
            request_timeout = min(request_timeout, remaining_budget())
        with httpx.Client(
            transport=self.transport,
            timeout=request_timeout, follow_redirects=False, trust_env=False,
            headers={"User-Agent": "MJobs-Discovery/1.0", "Accept": "text/html"},
        ) as client:
            active_response = []
            cancellation_errors = []
            stop_watcher = threading.Event()

            def interrupt_request():
                if active_response:
                    try:
                        active_response[0].close()
                    except Exception:
                        pass
                try:
                    client.close()
                except Exception:
                    pass

            def watch_cancellation():
                while not stop_watcher.wait(CANCELLATION_POLL_INTERVAL):
                    try:
                        self.check()
                    except BaseException as error:
                        cancellation_errors.append(error)
                        interrupt_request()
                        return

            watcher = threading.Thread(target=watch_cancellation,
                                       name="mjobs-testedrecruits-cancellation", daemon=True)
            watcher.start()
            try:
                with client.stream("GET", self.endpoint) as response:
                    active_response.append(response)
                    if response.is_redirect:
                        raise SourceError("SOURCE_REDIRECT_BLOCKED")
                    if response.status_code == 429:
                        raise SourceError("SOURCE_RATE_LIMITED")
                    if response.status_code != 200:
                        raise SourceError("SOURCE_HTTP_" + str(response.status_code))
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                        raise SourceError("SOURCE_INVALID_CONTENT_TYPE")
                    length = response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                        raise SourceError("SOURCE_PAYLOAD_LIMIT")
                    body_budget = max(5, min(BODY_BUDGET_MAX_SECONDS, self.request_timeout * 2))
                    if callable(remaining_budget):
                        body_budget = min(body_budget, remaining_budget())
                    deadline = time.monotonic() + body_budget
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        self.check()
                        content.extend(chunk)
                        if len(content) > MAX_BODY_BYTES:
                            raise SourceError("SOURCE_PAYLOAD_LIMIT")
                        if time.monotonic() > deadline:
                            raise SourceError("SOURCE_TIME_LIMIT")
                    self.check()
                    if cancellation_errors:
                        raise cancellation_errors[0]
                    if not content.rstrip().lower().endswith(b"</html>"):
                        raise SourceError("SOURCE_TRUNCATED")
                    return bytes(content)
            except httpx.TimeoutException as error:
                if cancellation_errors:
                    raise cancellation_errors[0]
                raise SourceError("SOURCE_TIMEOUT") from error
            except httpx.HTTPError as error:
                if cancellation_errors:
                    raise cancellation_errors[0]
                raise SourceError("SOURCE_NETWORK_ERROR") from error
            finally:
                stop_watcher.set()
                if watcher.is_alive():
                    watcher.join(timeout=CANCELLATION_POLL_INTERVAL)
            if cancellation_errors:
                raise cancellation_errors[0]
        raise SourceError("SOURCE_UNAVAILABLE")

    @staticmethod
    def _posting_id(href: str) -> str:
        if not isinstance(href, str):
            raise SourceError("SOURCE_INVALID_FORMAT")
        try:
            parsed = urlsplit(href)
            if (parsed.scheme != "https" or parsed.hostname != "app.testedrecruits.com"
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError
        except ValueError:
            raise SourceError("SOURCE_INVALID_FORMAT") from None
        match = _POSTING_PATH.fullmatch(parsed.path)
        if not match:
            raise SourceError("SOURCE_INVALID_FORMAT")
        return match.group(1)

    def search(self):
        with self._status_lock:
            self.status = "NOT_CHECKED"
        content = self._get_listing()
        soup = BeautifulSoup(content, "html.parser")
        count_matches = _COUNT.findall(soup.get_text(" ", strip=True))
        declared_count = int(count_matches[0].replace(",", "")) if len(count_matches) == 1 else None
        if declared_count is not None and declared_count > MAX_LISTINGS:
            raise SourceError("SOURCE_JOB_LIMIT")

        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        title = _plain_text(title, 200)
        company_match = re.fullmatch(r"(.+?)\s+Jobs\s*&\s*Careers", title, re.I)
        if not company_match or not company_match.group(1).strip():
            raise SourceError("SOURCE_INVALID_FORMAT")
        company_name = company_match.group(1).strip()
        # The vendor company number is the stable identity; its routing slug
        # may change without changing the employer account.
        company = SourceCompany(("testedrecruits", self.company_number), company_name, "")
        rows = []
        identifiers = set()
        malformed = len(count_matches) != 1
        for row in soup.find_all("tr"):
            self.check()
            title_cell = row.select_one("td.job-title")
            if title_cell is None:
                continue
            title_link = title_cell.find("a", href=True)
            descriptions = row.select("td.job-description")
            description = next((str(cell) for cell in descriptions
                                if cell.get_text(" ", strip=True)), "")
            if title_link is None:
                malformed = True
                continue
            posting_id = self._posting_id(title_link.get("href"))
            apply_cell = row.select_one("td.job-apply-btn")
            apply_link = apply_cell.find("a", href=True) if apply_cell else None
            if apply_link is None or self._posting_id(apply_link.get("href")) != posting_id:
                raise SourceError("SOURCE_INVALID_FORMAT")
            if posting_id in identifiers:
                malformed = True
                continue
            identifiers.add(posting_id)
            job_title = _plain_text(str(title_link), 500)
            if not job_title:
                malformed = True
                continue
            rows.append({
                "id": posting_id,
                "title": job_title,
                "description": _plain_text(description, 50_000),
                "company_name": company_name,
                "posting_url": f"{HOST}/posting/{posting_id}",
            })
            if len(rows) > MAX_LISTINGS:
                raise SourceError("SOURCE_JOB_LIMIT")

        has_pagination = any(
            ("next" in " ".join(anchor.get("rel", [])).casefold()
             or anchor.get_text(" ", strip=True).casefold() in {"next", "next page", "older"}
             or re.search(r"(?:[?&](?:page|p)=|/page/)", anchor.get("href", ""), re.I))
            for anchor in soup.find_all("a", href=True)
        )
        complete = (
            declared_count is not None and declared_count == len(rows)
            and not malformed and not has_pagination and len(identifiers) == len(rows)
        )
        items = tuple(SourceItem(raw, company) for raw in rows)
        with self._status_lock:
            self.status = "HEALTHY" if complete else "PARTIAL"
        return SearchBatch(self.endpoint, items, company, complete_inventory=complete)

    def fetch(self, job_id):
        if (isinstance(job_id, bool) or not isinstance(job_id, (int, str))
                or re.fullmatch(r"[0-9]+", str(job_id)) is None):
            raise ValueError("SOURCE_INVALID_JOB_ID")
        target = str(job_id)
        if int(target) <= 0:
            raise ValueError("SOURCE_INVALID_JOB_ID")
        batch = self.search()
        for item in batch.items:
            if item.raw["id"] == target:
                self.normalize(item.raw)
                return item.raw
        raise SourceError("SOURCE_JOB_NOT_FOUND")

    def normalize(self, raw):
        if not isinstance(raw, dict):
            raise SourceError("SOURCE_INVALID_JOB")
        posting_id = raw.get("id")
        title_raw = raw.get("title")
        if not isinstance(posting_id, str) or not _POSTING_PATH.fullmatch(f"/posting/{posting_id}"):
            raise SourceError("SOURCE_INVALID_JOB")
        if not isinstance(title_raw, str) or not title_raw.strip():
            raise SourceError("SOURCE_INVALID_JOB")
        title = _plain_text(title_raw, 500)
        description = structured_text(raw.get("description"))[:50_000]
        observed_company_name = raw.get("company_name")
        if not isinstance(observed_company_name, str) or not observed_company_name.strip():
            raise SourceError("SOURCE_INVALID_JOB")
        company_name = _plain_text(observed_company_name, 200)
        if not company_name:
            raise SourceError("SOURCE_INVALID_JOB")
        posting_url = raw.get("posting_url") or f"{HOST}/posting/{posting_id}"
        try:
            valid_posting_id = self._posting_id(posting_url)
        except SourceError:
            raise SourceError("SOURCE_INVALID_JOB") from None
        if valid_posting_id != posting_id:
            raise SourceError("SOURCE_INVALID_JOB")
        title_info = normalize_title(title)
        skill_data = extract_skills(f"{title} {description}")
        education = parse_education_requirement(description)
        requirements = parse_job_requirements(description)
        negatives = parse_negative_conditions(description, requirements["clearance"])
        salary = parse_salary("", "UNKNOWN")
        return {
            "requisition": posting_id,
            "title": title,
            "description": description,
            "skills": skill_data["labels"],
            "location": "Unknown",
            "remote_type": "UNKNOWN",
            "application_url": posting_url,
            "attributes": {
                "provider": "testedrecruits", "tenant": self.tenant,
                "company_name": company_name, "native_posting_id": int(posting_id),
                "native_job_id": int(posting_id), "lifecycle_claim": "ACTIVE",
                "employment_type": normalize_employment_type(""),
                "experience_level": normalize_experience_level(title)["level"],
                "experience_level_raw": normalize_experience_level(title)["raw"],
                "normalized_title": title_info["normalized_title"],
                "title_family": title_info["title_family"],
                "skill_ids": skill_data["ids"], "skill_mentions": skill_data["mentions"],
                "education_requirement": education["classification"],
                "education_requirement_raw": education["raw"],
                "experience_requirements": requirements["experience"],
                "certifications": requirements["certifications"],
                "clearance": requirements["clearance"],
                "work_authorization": requirements["work_authorization"],
                "negative_conditions": negatives,
                "salary_min": salary["salary_min"], "salary_max": salary["salary_max"],
                "salary_period": salary["salary_period"], "salary_currency": salary["currency"],
                "salary_source": salary["salary_source"], "salary_raw": None,
                "reported_posted_at": None, "reported_posted_at_raw": None,
                "remote_regions": [], "remote_indicated": False,
                "country": None, "state": None, "city": None,
                "skill_evidence": "listing_excerpt", "listing_description_only": True,
                "submission_capability": "MANUAL_ONLY", "fictional": False,
                "application_url_status": "PUBLIC_LISTING_LINK",
            },
        }
