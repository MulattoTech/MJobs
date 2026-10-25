"""Documented public GET adapter for We Work Remotely. No credentials or application submission.

Collects remote job listings from WWR's public RSS feed at
https://weworkremotely.com/remote-jobs.rss. Multi-company attribution,
canonical search-semantics normalization, error/rate-limit handling, and
source registry integration.

Requirement: MJ-REQ-017.S02
"""
from html import unescape
import re
import threading
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
import httpx

from services.discovery.sources.errors import SourceError
from services.discovery.sources.http_transport import SourceTransport
from services.discovery.sources.structured_text import structured_text

from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)
from services.discovery.search_semantics import (
    classify_location, normalize_employment_type, normalize_experience_level, normalize_title,
    parse_education_requirement, parse_salary, parse_timestamp,
)
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.negative_filters import parse_negative_conditions

HOST = "https://weworkremotely.com"
FEED_URL = "https://weworkremotely.com/remote-jobs.rss"
CANCELLATION_POLL_INTERVAL = 0.05




def plain_text(value):
    value = str(value or "")[:500_000]
    for _ in range(2):
        value = unescape(value)
    parsed = BeautifulSoup(value, "html.parser")
    for element in parsed(["script", "style", "noscript"]):
        element.decompose()
    return parsed.get_text(" ", strip=True)


def company_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").casefold()).strip("-")[:80] or "unknown"
    return slug


# ── Category normalization ─────────────────────────────────────────────
# WWR RSS <category> values → canonical form
_CATEGORY_MAP: dict[str, str] = {
    "full-stack programming": "Software Development",
    "front-end programming": "Software Development",
    "back-end programming": "Software Development",
    "programming": "Software Development",
    "design": "Design",
    "customer support": "Customer Support",
    "sales and marketing": "Sales & Marketing",
    "finance and legal": "Finance & Legal",
    "product": "Product",
    "business and management": "Business",
    "devops and sysadmin": "DevOps & Sysadmin",
    "human resources": "Human Resources",
    "copywriting": "Writing",
    "data": "Data",
    "all other remote": "All Others",
}

# ── Employment type normalization ──────────────────────────────────────
# WWR RSS <type> values → canonical form
_JOB_TYPE_MAP: dict[str, str] = {
    "full-time": "FULL_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "freelance": "FREELANCE",
    "internship": "INTERNSHIP",
}


def _extract_company_title(raw_title: str) -> tuple[str, str]:
    """Split WWR title format 'Company: Job Title' into (company, title).

    If the title does not contain a colon separator, the entire string is
    treated as the job title and company defaults to 'Unknown Company'.
    """
    if ":" in raw_title:
        company, _, title = raw_title.partition(":")
        company = company.strip()
        title = title.strip()
        if company and title:
            return company, title
    return "Unknown Company", raw_title.strip()


def _extract_skills_from_element(skills_text: str) -> list[str]:
    """Parse the comma-separated <skills> element into a list."""
    if not skills_text or not skills_text.strip():
        return []
    return [s.strip() for s in skills_text.split(",") if s.strip()]


def _posting_id_from_guid(guid: str) -> str:
    """Extract a stable posting identifier from the WWR guid/link URL.

    Example: https://weworkremotely.com/remote-jobs/lemon-io-senior-angular-full-stack-developer
    → 'lemon-io-senior-angular-full-stack-developer'
    """
    try:
        parsed = urlsplit(guid)
        path = parsed.path.rstrip("/")
        if path.startswith("/remote-jobs/"):
            return path[len("/remote-jobs/"):]
        # Fallback: use last path segment
        return path.rsplit("/", 1)[-1] if "/" in path else path
    except Exception:
        return guid


class WeWorkRemotely:
    """We Work Remotely source adapter using their public RSS feed.

    Supports DISCOVERY, JOB_DETAIL, REMOTE_METADATA and APPLICATION_REDIRECT
    capabilities via the FEED collection strategy. No credentials, browser
    evasion, user application POSTs or arbitrary URL fetching.
    """

    def __init__(self, tenant="default", transport=None, check=None, request_timeout=10, rate_limit=60, concurrency=1):
        self.tenant = str(tenant or "default").strip().casefold()
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
        self._last_request = None
        self._rate_limit_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._request_failed = False
        self.status = "NOT_CHECKED"

    def capabilities(self):
        return SourceCapabilities(CollectionStrategy.FEED, frozenset({
            SourceCapability.DISCOVERY, SourceCapability.JOB_DETAIL,
            SourceCapability.REMOTE_METADATA, SourceCapability.APPLICATION_REDIRECT,
        }))

    def health(self):
        with self._status_lock:
            return self.status

    def _reset_request_status(self):
        with self._status_lock:
            self.status = "NOT_CHECKED"
            self._request_failed = False

    def _wait_for_rate_limit(self):
        interval = 60.0 / self.rate_limit
        with self._rate_limit_lock:
            now_mono = time.monotonic()
            scheduled = now_mono if self._last_request is None else max(now_mono, self._last_request + interval)
            self._last_request = scheduled
        wait = scheduled - time.monotonic()
        while wait > 0:
            self.check()
            time.sleep(min(wait, CANCELLATION_POLL_INTERVAL))
            wait = scheduled - time.monotonic()
        self.check()

    def _request(self):
        """Perform rate-limited GET against the WWR RSS feed and return parsed XML."""
        self.check()
        if self.transport is None:
            self._wait_for_rate_limit()

        remaining_budget = getattr(self.check, "remaining_seconds", None)
        request_timeout = float(self.request_timeout)
        if callable(remaining_budget):
            request_timeout = min(request_timeout, remaining_budget())

        with httpx.Client(transport=self.transport if self.transport is not None else SourceTransport(self.check), timeout=request_timeout,
                          follow_redirects=False, trust_env=False,
                          headers={"User-Agent": "MJobs-Discovery/1.0",
                                   "Accept": "application/rss+xml, application/xml, text/xml"}) as client:
            stop_watcher = threading.Event()
            cancellation_errors = []
            active_response = []

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
                                       name="mjobs-wwr-cancellation", daemon=True)
            watcher.start()
            try:
                try:
                    with client.stream("GET", FEED_URL) as response:
                        active_response.append(response)
                        if response.status_code == 429:
                            retry_after = 60
                            try:
                                retry_after = max(int(response.headers.get("retry-after", "60")), 1)
                            except (ValueError, TypeError):
                                pass
                            with self._status_lock:
                                self.status = "SOURCE_RATE_LIMITED"
                                self._request_failed = True
                            raise SourceError("SOURCE_RATE_LIMITED", retry_seconds=retry_after)

                        if response.status_code >= 500:
                            with self._status_lock:
                                self.status = "SOURCE_SERVER_ERROR"
                                self._request_failed = True
                            raise SourceError("SOURCE_SERVER_ERROR", retry_seconds=300)

                        if response.status_code != 200:
                            with self._status_lock:
                                self.status = "SOURCE_UNAVAILABLE"
                                self._request_failed = True
                            raise SourceError("SOURCE_UNAVAILABLE", retry_seconds=300)

                        content = bytearray()
                        body_budget = max(5, min(120, self.request_timeout * 2))
                        if callable(remaining_budget):
                            body_budget = min(body_budget, remaining_budget())
                        deadline = time.monotonic() + body_budget
                        for chunk in response.iter_bytes(chunk_size=65536):
                            self.check()
                            content.extend(chunk)
                            if len(content) > 5_000_000:
                                with self._status_lock:
                                    self.status = "SOURCE_PAYLOAD_LIMIT"
                                    self._request_failed = True
                                raise SourceError("SOURCE_PAYLOAD_LIMIT")
                            if time.monotonic() > deadline:
                                with self._status_lock:
                                    self.status = "SOURCE_TIME_LIMIT"
                                    self._request_failed = True
                                raise SourceError("SOURCE_TIME_LIMIT")
                        self.check()
                        if cancellation_errors:
                            raise cancellation_errors[0]
                        try:
                            root = ElementTree.fromstring(content)
                        except ElementTree.ParseError as exc:
                            with self._status_lock:
                                self.status = "SOURCE_PAYLOAD_INVALID"
                                self._request_failed = True
                            raise SourceError("SOURCE_PAYLOAD_INVALID") from exc
                except httpx.TimeoutException as exc:
                    if cancellation_errors:
                        raise cancellation_errors[0]
                    with self._status_lock:
                        self.status = "SOURCE_TIMEOUT"
                        self._request_failed = True
                    raise SourceError("SOURCE_TIMEOUT", retry_seconds=120) from exc
                except httpx.HTTPError as exc:
                    if cancellation_errors:
                        raise cancellation_errors[0]
                    with self._status_lock:
                        self.status = "SOURCE_NETWORK_ERROR"
                        self._request_failed = True
                    raise SourceError("SOURCE_NETWORK_ERROR", retry_seconds=120) from exc
            except BaseException:
                if cancellation_errors:
                    raise cancellation_errors[0]
                raise
            finally:
                stop_watcher.set()
                if watcher.is_alive():
                    watcher.join(timeout=1)

            if cancellation_errors:
                raise cancellation_errors[0]
            self.check()

            channel = root.find("channel")
            if channel is None:
                with self._status_lock:
                    self.status = "SOURCE_PAYLOAD_INVALID"
                    self._request_failed = True
                raise SourceError("SOURCE_PAYLOAD_INVALID")

            with self._status_lock:
                if not self._request_failed:
                    self.status = "HEALTHY"

            return channel

    def _parse_items(self, channel) -> list[dict]:
        """Parse RSS <item> elements into raw dicts."""
        items = []
        for item in channel.findall("item"):
            raw = {}
            raw["title"] = (item.findtext("title") or "").strip()
            raw["link"] = (item.findtext("link") or "").strip()
            raw["guid"] = (item.findtext("guid") or "").strip()
            raw["description"] = (item.findtext("description") or "").strip()
            raw["pubDate"] = (item.findtext("pubDate") or "").strip()
            raw["region"] = (item.findtext("region") or "").strip()
            raw["country"] = (item.findtext("country") or "").strip()
            raw["state"] = (item.findtext("state") or "").strip()
            raw["category"] = (item.findtext("category") or "").strip()
            raw["type"] = (item.findtext("type") or "").strip()
            raw["skills"] = (item.findtext("skills") or "").strip()
            raw["expires_at"] = (item.findtext("expires_at") or "").strip()

            # media:content logo URL
            media_ns = "http://search.yahoo.com/mrss"
            media_content = item.find(f"{{{media_ns}}}content")
            raw["logo_url"] = (media_content.get("url", "") if media_content is not None else "").strip()

            if raw["title"] and (raw["link"] or raw["guid"]):
                items.append(raw)
        return items

    def search(self, category=None, search_term=None, limit=None):
        """Collect jobs from the WWR public RSS feed.

        The RSS feed does not support server-side filtering, so category and
        search_term filtering is applied client-side on the full feed result.
        """
        self._reset_request_status()
        channel = self._request()
        all_items = self._parse_items(channel)

        # Client-side filtering (RSS does not support query params)
        if category:
            cat_lower = str(category).strip().casefold()
            all_items = [item for item in all_items if cat_lower in item.get("category", "").casefold()]
        if search_term:
            term_lower = str(search_term).strip().casefold()
            all_items = [item for item in all_items
                         if term_lower in item.get("title", "").casefold()
                         or term_lower in item.get("description", "").casefold()]
        if limit is not None:
            if type(limit) is not int or limit <= 0:
                raise ValueError("SOURCE_INVALID_LIMIT")
            all_items = all_items[:limit]

        source_items = []
        for raw in all_items:
            company_name, _ = _extract_company_title(raw["title"])
            slug = company_slug(company_name)
            company = SourceCompany(
                identity_parts=("wwr", slug),
                name=company_name,
                domain="https://weworkremotely.com",
            )
            source_items.append(SourceItem(raw, company))

        return SearchBatch(
            endpoint=FEED_URL,
            items=tuple(source_items),
            source_company=None,
            complete_inventory=False,
        )

    def fetch(self, job_id):
        """Fetch a specific job by its posting slug/guid.

        The RSS feed is the only public data source, so fetch re-searches and
        filters to the requested job. This is consistent with the Remotive pattern.
        """
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("SOURCE_INVALID_JOB_ID")
        self._reset_request_status()
        batch = self.search()
        for item in batch.items:
            posting_id = _posting_id_from_guid(item.raw.get("guid") or item.raw.get("link", ""))
            if posting_id == job_id:
                self.normalize(item.raw)
                return item.raw
        raise SourceError("SOURCE_JOB_NOT_FOUND")

    def normalize(self, raw):
        """Normalize a raw WWR RSS item dict to the canonical job representation."""
        if not isinstance(raw, dict) or not isinstance(raw.get("title"), str) or not raw["title"].strip():
            raise SourceError("SOURCE_INVALID_JOB")

        guid = raw.get("guid") or raw.get("link") or ""
        posting_slug = _posting_id_from_guid(guid)
        if not posting_slug:
            raise SourceError("SOURCE_INVALID_JOB")

        company_name_raw, title_raw = _extract_company_title(raw["title"])
        title = plain_text(title_raw)[:500]
        description = structured_text(raw.get("description"))[:50000]
        company_name = plain_text(company_name_raw)[:200]

        # Location from region, country, state
        region = (raw.get("region") or "").strip()
        country = (raw.get("country") or "").strip()
        state = (raw.get("state") or "").strip()
        location_raw = region or country or "Remote"
        location_facts = classify_location(location_raw, description)

        # Skills from <skills> element and description
        skills_list = _extract_skills_from_element(raw.get("skills", ""))
        skills_str = " ".join(skills_list)
        combined = f"{title} {description} {skills_str}"

        # Employment type from <type>
        raw_type = (raw.get("type") or "").strip().casefold()
        wwr_type = _JOB_TYPE_MAP.get(raw_type)
        employment_type = wwr_type or normalize_employment_type(raw_type or combined)
        experience = normalize_experience_level(title)
        title_info = normalize_title(title)
        education = parse_education_requirement(description)
        skill_data = extract_skills(combined)

        # Application URL: the WWR link to the job posting
        url = raw.get("link") or raw.get("guid") or ""
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or "weworkremotely.com" not in (parsed.hostname or ""):
                url = ""
            if parsed.username or parsed.password:
                url = ""
        except ValueError:
            url = ""

        # Category normalization
        category_raw = (raw.get("category") or "").strip()
        category = _CATEGORY_MAP.get(category_raw.casefold(), category_raw or None)

        # Salary: parse from description (WWR does not have a dedicated salary field)
        salary = parse_salary(description, "SOURCE_PROVIDED")

        # Published date
        reported = parse_timestamp(raw.get("pubDate"))

        # Expiry
        expires_at = parse_timestamp(raw.get("expires_at"))

        requirements = parse_job_requirements(description)
        negative_conditions = parse_negative_conditions(description, requirements["clearance"])

        return {
            "requisition": posting_slug,
            "title": title,
            "description": description,
            "skills": skill_data["labels"],
            "location": location_raw,
            "remote_type": "REMOTE",
            "application_url": url,
            "attributes": {
                "provider": "wwr",
                "tenant": self.tenant,
                "company_name": company_name,
                "native_posting_id": posting_slug,
                "native_job_id": posting_slug,
                "lifecycle_claim": "ACTIVE",
                "category": category,
                "category_raw": category_raw,
                "skills_raw": skills_list,
                "employment_type": employment_type,
                "experience_level": experience["level"],
                "experience_level_raw": experience["raw"],
                "normalized_title": title_info["normalized_title"],
                "title_family": title_info["title_family"],
                "skill_ids": skill_data["ids"],
                "skill_mentions": skill_data["mentions"],
                "education_requirement": education["classification"],
                "education_requirement_raw": education["raw"],
                "experience_requirements": requirements["experience"],
                "certifications": requirements["certifications"],
                "clearance": requirements["clearance"],
                "work_authorization": requirements["work_authorization"],
                "negative_conditions": negative_conditions,
                "salary_min": salary["salary_min"],
                "salary_max": salary["salary_max"],
                "salary_period": salary["salary_period"],
                "salary_currency": salary["currency"],
                "salary_source": salary["salary_source"],
                "salary_raw": salary["salary_raw"],
                "reported_posted_at": reported.isoformat() if reported else None,
                "reported_posted_at_raw": raw.get("pubDate"),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "remote_regions": location_facts["remote_regions"],
                "remote_indicated": True,
                "country": location_facts["country"],
                "state": location_facts["state"],
                "city": location_facts["city"],
                "region_raw": region,
                "country_raw": country,
                "state_raw": state,
                "skill_evidence": "description_mentions",
                "submission_capability": "MANUAL_ONLY",
                "fictional": False,
                "application_url_status": "PROVIDER_SUPPLIED" if url else "UNAVAILABLE",
                "logo_url": raw.get("logo_url") or None,
            },
        }
