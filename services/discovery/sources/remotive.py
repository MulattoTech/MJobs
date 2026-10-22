"""Documented public GET adapter for Remotive. No credentials or application submission."""
from html import unescape
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit

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

HOST = "https://remotive.com"
API_URL = "https://remotive.com/api/remote-jobs"
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


class Remotive:
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
        return SourceCapabilities(CollectionStrategy.PUBLIC_JSON, frozenset({
            SourceCapability.DISCOVERY, SourceCapability.JOB_DETAIL,
            SourceCapability.SALARY, SourceCapability.REMOTE_METADATA,
            SourceCapability.APPLICATION_REDIRECT,
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

    def _request(self, params=None):
        self.check()
        if self.transport is None:
            self._wait_for_rate_limit()

        remaining_budget = getattr(self.check, "remaining_seconds", None)
        request_timeout = float(self.request_timeout)
        if callable(remaining_budget):
            request_timeout = min(request_timeout, remaining_budget())

        with httpx.Client(transport=self.transport if self.transport is not None else SourceTransport(self.check), timeout=request_timeout,
                          follow_redirects=False, trust_env=False,
                          headers={"User-Agent": "MJobs-Discovery/1.0", "Accept": "application/json"}) as client:
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
                                       name="mjobs-remotive-cancellation", daemon=True)
            watcher.start()
            try:
                try:
                    with client.stream("GET", API_URL, params=params) as response:
                        active_response.append(response)
                        if response.is_redirect:
                            with self._status_lock:
                                self.status = "SOURCE_REDIRECT_BLOCKED"
                                self._request_failed = True
                            raise SourceError("SOURCE_REDIRECT_BLOCKED")

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
                        # Do not let HTTPX aggregate a 64 KiB body chunk before
                        # observing cancellation/deadline checks. Slow-drip
                        # responses may otherwise postpone the worker check for
                        # many seconds while yielding individually small parts.
                        for chunk in response.iter_bytes(chunk_size=None):
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
                            payload = json.loads(content)
                        except Exception as exc:
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

            if not isinstance(payload, dict) or "jobs" not in payload or not isinstance(payload["jobs"], list):
                with self._status_lock:
                    self.status = "SOURCE_PAYLOAD_INVALID"
                    self._request_failed = True
                raise SourceError("SOURCE_PAYLOAD_INVALID")

            with self._status_lock:
                if not self._request_failed:
                    self.status = "HEALTHY"

            return payload

    def search(self, category=None, search_term=None, limit=None):
        self._reset_request_status()
        params = {}
        if category:
            params["category"] = str(category).strip()
        if search_term:
            params["search"] = str(search_term).strip()
        if limit is not None:
            if type(limit) is not int or limit <= 0:
                raise ValueError("SOURCE_INVALID_LIMIT")
            params["limit"] = str(limit)

        payload = self._request(params=params or None)
        jobs = payload["jobs"]
        items = []
        for raw in jobs:
            if not isinstance(raw, dict):
                continue
            company_name = plain_text(raw.get("company_name") or "Unknown Company")
            slug = company_slug(company_name)
            company = SourceCompany(
                identity_parts=("remotive", slug),
                name=company_name,
                domain="https://remotive.com",
            )
            items.append(SourceItem(raw, company))

        return SearchBatch(
            endpoint=API_URL,
            items=tuple(items),
            source_company=None,
            complete_inventory=False,
        )

    def fetch(self, job_id):
        if type(job_id) is not int or job_id <= 0:
            raise ValueError("SOURCE_INVALID_JOB_ID")
        self._reset_request_status()
        batch = self.search()
        for item in batch.items:
            if item.raw.get("id") == job_id:
                self.normalize(item.raw)
                return item.raw
        raise SourceError("SOURCE_JOB_NOT_FOUND")

    def normalize(self, raw):
        if not isinstance(raw, dict) or type(raw.get("id")) not in (int, str) or not str(raw.get("id")).strip():
            raise SourceError("SOURCE_INVALID_JOB")
        native_id = int(raw["id"]) if isinstance(raw["id"], int) or (isinstance(raw["id"], str) and raw["id"].isdigit()) else raw["id"]
        if not isinstance(raw.get("title"), str) or not raw["title"].strip():
            raise SourceError("SOURCE_INVALID_JOB")

        title = plain_text(raw["title"])[:500]
        description = structured_text(raw.get("description"))[:50000]
        company_name = plain_text(raw.get("company_name") or "Unknown Company")[:200]
        location_raw = plain_text(raw.get("candidate_required_location") or "Remote")[:1000]
        location_facts = classify_location(location_raw, description)

        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        tag_str = " ".join(str(t) for t in tags if isinstance(t, str))
        combined = f"{title} {description} {tag_str}"

        raw_type = str(raw.get("job_type") or "").replace("_", "-")
        employment_type = normalize_employment_type(raw_type or combined)
        experience = normalize_experience_level(title)
        title_info = normalize_title(title)
        education = parse_education_requirement(description)
        skill_data = extract_skills(combined)

        url = raw.get("url") if isinstance(raw.get("url"), str) else ""
        try:
            parsed = urlsplit(url)
            # Only the adapter's canonical HTTPS origin is provider-supplied.
            # Reject parser/browser ambiguities rather than repairing source data.
            if (parsed.scheme != "https" or parsed.hostname != "remotive.com"
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443)
                    or "\\" in url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
                url = ""
        except ValueError:
            url = ""

        salary_text = str(raw.get("salary") or "")
        salary = parse_salary(salary_text or description, "EMPLOYER_PROVIDED" if salary_text else "SOURCE_PROVIDED")

        reported = parse_timestamp(raw.get("publication_date"))
        requirements = parse_job_requirements(description)
        negative_conditions = parse_negative_conditions(description, requirements["clearance"])

        return {
            "requisition": str(native_id),
            "title": title,
            "description": description,
            "skills": skill_data["labels"],
            "location": location_raw,
            "remote_type": "REMOTE",
            "application_url": url,
            "attributes": {
                "provider": "remotive",
                "tenant": self.tenant,
                "company_name": company_name,
                "native_posting_id": native_id,
                "native_job_id": native_id,
                "lifecycle_claim": "ACTIVE",
                "category": raw.get("category"),
                "tags": tags,
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
                "reported_posted_at_raw": raw.get("publication_date"),
                "remote_regions": location_facts["remote_regions"],
                "remote_indicated": True,
                "country": location_facts["country"],
                "state": location_facts["state"],
                "city": location_facts["city"],
                "skill_evidence": "description_mentions",
                "submission_capability": "MANUAL_ONLY",
                "fictional": False,
                "application_url_status": "PROVIDER_SUPPLIED" if url else "UNAVAILABLE",
            },
        }
