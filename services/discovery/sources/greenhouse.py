"""Documented public GET adapter. No credentials or application submission."""
from concurrent.futures import ThreadPoolExecutor
from html import unescape
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

HOST = "https://boards-api.greenhouse.io"
CANCELLATION_POLL_INTERVAL = 0.05




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
        self._last_request = None
        self._rate_limit_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._request_failed = False
        self.status = "NOT_CHECKED"

    def capabilities(self):
        return SourceCapabilities(CollectionStrategy.API, frozenset({
            SourceCapability.DISCOVERY, SourceCapability.JOB_DETAIL,
            SourceCapability.SALARY, SourceCapability.REMOTE_METADATA,
            SourceCapability.APPLICATION_REDIRECT,
        }))

    def health(self):
        with self._status_lock:
            return self.status

    def _get(self, suffix, params=None):
        try:
            return self._request(suffix, params)
        except SourceError as failure:
            with self._status_lock:
                self.status = failure.code
                self._request_failed = True
            raise
        except httpx.HTTPError:
            with self._status_lock:
                self.status = "SOURCE_NETWORK_ERROR"
                self._request_failed = True
            raise

    def _reset_request_status(self):
        with self._status_lock:
            self.status = "NOT_CHECKED"
            self._request_failed = False

    def _request_url(self, suffix):
        return HOST + "/v1/boards/" + self.tenant + suffix

    def _request(self, suffix, params=None):
        self.check()
        if self.transport is None:
            self._wait_for_rate_limit()
        remaining_budget = getattr(self.check, "remaining_seconds", None)
        request_timeout = self.request_timeout
        if callable(remaining_budget):
            request_timeout = min(request_timeout, remaining_budget())
        with httpx.Client(timeout=request_timeout, follow_redirects=False, trust_env=False,
                          transport=self.transport if self.transport is not None else SourceTransport(self.check),
                          headers={"User-Agent": "MJobs/0.1 public-read-only", "Accept": "application/json"}) as client:
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
                                       name="mjobs-greenhouse-cancellation", daemon=True)
            watcher.start()
            try:
                with client.stream("GET", self._request_url(suffix), params=params) as response:
                    active_response.append(response)
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
                    body_budget = max(5, min(120, self.request_timeout * 2))
                    if callable(remaining_budget):
                        body_budget = min(body_budget, remaining_budget())
                    deadline = time.monotonic() + body_budget
                    for chunk in response.iter_bytes(chunk_size=65536):
                        self.check()
                        content.extend(chunk)
                        if len(content) > 5_000_000:
                            raise SourceError("SOURCE_PAYLOAD_LIMIT")
                        if time.monotonic() > deadline:
                            raise SourceError("SOURCE_TIME_LIMIT")
                    self.check()
                    if cancellation_errors:
                        raise cancellation_errors[0]
                    try:
                        payload = __import__("json").loads(content)
                    except ValueError as error:
                        raise SourceError("SOURCE_INVALID_JSON") from error
            except BaseException:
                if cancellation_errors:
                    raise cancellation_errors[0]
                raise
            finally:
                stop_watcher.set()
                if watcher.is_alive():
                    # Closing an HTTPX response/client may contend with the
                    # active request. Do not let that cleanup extend prompt
                    # worker cancellation by a full second.
                    watcher.join(timeout=CANCELLATION_POLL_INTERVAL)
            if cancellation_errors:
                raise cancellation_errors[0]
            self.check()
            with self._status_lock:
                if not self._request_failed:
                    self.status = "HEALTHY"
            return payload

    def _wait_for_rate_limit(self):
        interval = 60 / self.rate_limit
        # Reserve each request start atomically. Parallel callers share the
        # source's rate schedule instead of independently passing a per-thread
        # interval check and creating a burst.
        with self._rate_limit_lock:
            now = time.monotonic()
            scheduled = now if self._last_request is None else max(now, self._last_request + interval)
            self._last_request = scheduled
        wait = scheduled - time.monotonic()
        while wait > 0:
            self.check()
            time.sleep(min(wait, 0.25))
            wait = scheduled - time.monotonic()
        self.check()

    def search(self):
        self._reset_request_status()
        # Board metadata and the bounded job inventory are independent public
        # GETs. Run them under the configured per-source cap; the cap is an
        # upper bound, and this search currently has two independent requests.
        with ThreadPoolExecutor(max_workers=min(self.concurrency, 2),
                               thread_name_prefix="mjobs-greenhouse") as executor:
            board_request = executor.submit(self._get, "")
            jobs_request = executor.submit(self._get, "/jobs", {"content": "true"})
            failures = []
            try:
                board = board_request.result()
            except Exception as failure:
                failures.append(failure)
                board = None
            try:
                payload = jobs_request.result()
            except Exception as failure:
                failures.append(failure)
                payload = None
            if failures:
                # Both requests have now settled. Recheck cancellation/deadline
                # so a concurrent provider error cannot turn a local stop into
                # a provider failure or poison source health/history.
                self.check()
                raise failures[0]
        if not isinstance(board, dict) or not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise SourceError("SOURCE_INVALID_FORMAT")
        if len(payload["jobs"]) > 1000:
            raise SourceError("SOURCE_JOB_LIMIT")
        company = SourceCompany(("greenhouse", self.tenant), plain_text(board.get("name") or self.tenant)[:200],
                                "https://job-boards.greenhouse.io/" + self.tenant)
        meta = payload.get('meta')
        total = meta.get('total') if isinstance(meta, dict) else None
        complete = 'meta' not in payload or (type(total) is int and total == len(payload['jobs']))
        return SearchBatch(HOST + "/v1/boards/" + self.tenant + "/jobs",
                           tuple(SourceItem(raw, company) for raw in payload["jobs"]), company, complete_inventory=complete)

    def collect(self):
        """Compatibility for callers of the original bounded board collection API."""
        batch = self.search()
        return {"company": batch.source_company.name if batch.source_company else self.tenant,
                "jobs": [item.raw for item in batch.items], "endpoint": batch.endpoint}

    def fetch(self, job_id):
        if type(job_id) is not int or job_id <= 0:
            raise ValueError("SOURCE_INVALID_JOB_ID")
        self._reset_request_status()
        raw = self._get("/jobs/" + str(job_id), {"pay_transparency": "true"})
        self.normalize(raw)
        if raw["id"] != job_id:
            raise SourceError("SOURCE_JOB_ID_MISMATCH")
        return raw

    def normalize(self, raw):
        if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0 or not isinstance(raw.get("title"), str) or not raw["title"].strip():
            raise SourceError("SOURCE_INVALID_JOB")
        description = structured_text(raw.get("content"))[:50000]
        if raw.get("location") and not isinstance(raw["location"], dict):
            raise SourceError("SOURCE_INVALID_JOB_LOCATION")
        location = plain_text((raw.get("location") or {}).get("name") or "Unknown")[:1000]
        location_facts = classify_location(location, description)
        title = plain_text(raw["title"])[:500]
        combined = title + " " + description
        employment_type = normalize_employment_type(combined)
        experience = normalize_experience_level(title)
        title_info = normalize_title(title)
        education = parse_education_requirement(description)
        skill_data = extract_skills(combined)
        url = str(raw.get("absolute_url") or "")
        if url:
            try:
                parsed = urlsplit(url)
                path = parsed.path.split("/")
                if (parsed.scheme != "https"
                        or parsed.hostname not in ("boards.greenhouse.io", "job-boards.greenhouse.io")
                        or parsed.username or parsed.password or parsed.port not in (None, 443)
                        or len(path) < 2 or path[1].casefold() != self.tenant):
                    raise SourceError("COMPANY_ATS_JOB_TENANT_MISMATCH")
            except ValueError:
                raise SourceError("COMPANY_ATS_JOB_TENANT_MISMATCH") from None
        ranges = []
        for row in (raw.get("pay_input_ranges") if isinstance(raw.get("pay_input_ranges"), list) else []):
            if not isinstance(row, dict):
                continue
            lower, upper = row.get("min_cents"), row.get("max_cents")
            if all(type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1e11 for value in (lower, upper)) and lower <= upper:
                ranges.append({"min": lower / 100, "max": upper / 100, "currency": str(row.get("currency_type") or "Unknown")[:8], "period": "UNKNOWN"})
        if ranges:
            currencies = {row["currency"] for row in ranges}
            salary = {"salary_min": min(row["min"] for row in ranges), "salary_max": max(row["max"] for row in ranges),
                      "salary_period": "UNKNOWN", "currency": next(iter(currencies)) if len(currencies) == 1 else None,
                      "salary_source": "EMPLOYER_PROVIDED", "salary_raw": ranges}
        else:
            salary = parse_salary(description, "EMPLOYER_PROVIDED")
        reported = parse_timestamp(raw.get("first_published_at") or raw.get("published_at"))
        requirements = parse_job_requirements(description)
        negative_conditions = parse_negative_conditions(description, requirements["clearance"])
        return {"requisition": str(raw["id"]), "title": title, "description": description,
                "skills": skill_data["labels"], "location": location, "remote_type": location_facts["remote_type"], "application_url": url,
                "attributes": {"provider": "greenhouse", "tenant": self.tenant, "native_job_id": raw.get("internal_job_id"),
                    "lifecycle_claim": "ACTIVE",
                    "native_posting_id": raw["id"], "updated_at": raw.get("updated_at"), "salary": ranges or None,
                    "employment_type": employment_type, "experience_level": experience["level"],
                    "experience_level_raw": experience["raw"], "normalized_title": title_info["normalized_title"],
                    "title_family": title_info["title_family"], "skill_ids": skill_data["ids"],
                    "skill_mentions": skill_data["mentions"], "education_requirement": education["classification"],
                    "education_requirement_raw": education["raw"],
                    "experience_requirements": requirements["experience"],
                    "certifications": requirements["certifications"],
                    "clearance": requirements["clearance"],
                    "work_authorization": requirements["work_authorization"],
                    "negative_conditions": negative_conditions,
                    "salary_min": salary["salary_min"],
                    "salary_max": salary["salary_max"], "salary_period": salary["salary_period"],
                    "salary_currency": salary["currency"], "salary_source": salary["salary_source"],
                    "salary_raw": salary["salary_raw"], "reported_posted_at": reported.isoformat() if reported else None,
                    "reported_posted_at_raw": raw.get("first_published_at") or raw.get("published_at"),
                    "remote_regions": location_facts["remote_regions"],
                    "remote_indicated": location_facts["remote_indicated"], "country": location_facts["country"],
                    "state": location_facts["state"], "city": location_facts["city"],
                    "skill_evidence": "description_mentions", "submission_capability": "MANUAL_ONLY", "fictional": False,
                    "application_url_status": "PROVIDER_SUPPLIED" if url else "UNAVAILABLE"}}
