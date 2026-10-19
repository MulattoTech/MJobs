"""Documented public GET adapter for Remote OK. No credentials or application submission.

Requirement: MJ-REQ-017.S07
"""
from datetime import datetime, timezone
from html import unescape
import json
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

HOST = "https://remoteok.com"
API_URL = "https://remoteok.com/api"
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


class RemoteOK:
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
            SourceCapability.DISCOVERY,
            SourceCapability.JOB_DETAIL,
            SourceCapability.SALARY,
            SourceCapability.REMOTE_METADATA,
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
                                       name="mjobs-remoteok-cancellation", daemon=True)
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

            if not isinstance(payload, list):
                with self._status_lock:
                    self.status = "SOURCE_PAYLOAD_INVALID"
                    self._request_failed = True
                raise SourceError("SOURCE_PAYLOAD_INVALID")

            with self._status_lock:
                if not self._request_failed:
                    self.status = "HEALTHY"

            return payload

    def search(self, tag=None, search_term=None, limit=None):
        self._reset_request_status()
        params = {}
        target_tag = tag or search_term
        if target_tag:
            params["tag"] = str(target_tag).strip()
        if limit is not None:
            if type(limit) is not int or limit <= 0:
                raise ValueError("SOURCE_INVALID_LIMIT")

        payload = self._request(params=params or None)
        items = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            # Legal notice or metadata dict (e.g. index 0) does not contain a job id
            if not raw.get("id"):
                continue
            if not (raw.get("position") or raw.get("title")):
                continue

            company_name = plain_text(raw.get("company") or "Unknown Company")
            slug = company_slug(company_name)
            company = SourceCompany(
                identity_parts=("remoteok", slug),
                name=company_name,
                domain="https://remoteok.com",
            )
            items.append(SourceItem(raw, company))

        if limit is not None:
            items = items[:limit]

        return SearchBatch(
            endpoint=API_URL,
            items=tuple(items),
            source_company=None,
            complete_inventory=False,
        )

    def fetch(self, job_id):
        if isinstance(job_id, int):
            if job_id <= 0:
                raise ValueError("SOURCE_INVALID_JOB_ID")
            str_id = str(job_id)
        elif isinstance(job_id, str):
            str_id = job_id.strip()
            if not str_id:
                raise ValueError("SOURCE_INVALID_JOB_ID")
        else:
            raise ValueError("SOURCE_INVALID_JOB_ID")

        self._reset_request_status()
        batch = self.search()
        for item in batch.items:
            item_id = str(item.raw.get("id", "")).strip()
            if item_id == str_id:
                self.normalize(item.raw)
                return item.raw
        raise SourceError("SOURCE_JOB_NOT_FOUND")

    def normalize(self, raw):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise SourceError("SOURCE_INVALID_JOB")
        raw_id = raw["id"]
        if isinstance(raw_id, int):
            native_id = raw_id
        elif isinstance(raw_id, str) and raw_id.strip().isdigit():
            native_id = int(raw_id.strip())
        elif isinstance(raw_id, str) and raw_id.strip():
            native_id = raw_id.strip()
        else:
            raise SourceError("SOURCE_INVALID_JOB")

        title_raw = raw.get("position") or raw.get("title")
        if not isinstance(title_raw, str) or not title_raw.strip():
            raise SourceError("SOURCE_INVALID_JOB")

        title = plain_text(title_raw)[:500]
        description = structured_text(raw.get("description"))[:50000]
        company_name = plain_text(raw.get("company") or "Unknown Company")[:200]
        location_raw = plain_text(raw.get("location") or "Remote")[:1000] or "Remote"
        location_facts = classify_location(location_raw, description)

        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        clean_tags = [str(t).strip() for t in tags if isinstance(t, str) and str(t).strip()]
        tag_str = " ".join(clean_tags)
        combined = f"{title} {description} {tag_str}"

        # Detect employment type from tags or combined text
        employment_type = normalize_employment_type(tag_str or combined)
        experience = normalize_experience_level(title)
        title_info = normalize_title(title)
        education = parse_education_requirement(description)
        skill_data = extract_skills(combined)

        candidate_url = str(raw.get("apply_url") or raw.get("url") or "")
        url = ""
        try:
            parsed = urlsplit(candidate_url)
            if parsed.scheme in ("https", "http") and parsed.hostname and not (parsed.username or parsed.password):
                url = candidate_url
        except ValueError:
            url = ""

        # Parse salary
        salary_min_val = None
        salary_max_val = None
        try:
            raw_smin = raw.get("salary_min")
            if raw_smin is not None and float(raw_smin) > 0:
                salary_min_val = float(raw_smin)
        except (ValueError, TypeError):
            pass
        try:
            raw_smax = raw.get("salary_max")
            if raw_smax is not None and float(raw_smax) > 0:
                salary_max_val = float(raw_smax)
        except (ValueError, TypeError):
            pass

        if salary_min_val and salary_max_val and salary_min_val <= salary_max_val:
            salary_text = f"${int(salary_min_val)} - ${int(salary_max_val)}"
        elif salary_min_val:
            salary_text = f"starting at ${int(salary_min_val)}"
        elif salary_max_val:
            salary_text = f"up to ${int(salary_max_val)}"
        else:
            salary_text = ""

        salary = parse_salary(salary_text or description, "SOURCE_PROVIDED")
        if salary_text and salary["salary_period"] == "UNKNOWN":
            salary["salary_period"] = "YEAR"

        # Posted timestamp
        reported = parse_timestamp(raw.get("date"))
        if not reported and isinstance(raw.get("epoch"), (int, float)) and raw["epoch"] > 0:
            try:
                reported = datetime.fromtimestamp(raw["epoch"], tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                reported = None

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
                "provider": "remoteok",
                "tenant": self.tenant,
                "company_name": company_name,
                "native_posting_id": native_id,
                "native_job_id": native_id,
                "lifecycle_claim": "ACTIVE",
                "category": raw.get("category"),
                "tags": clean_tags,
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
                "reported_posted_at_raw": raw.get("date"),
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
