"""Bounded public USAJOBS IT discovery; no credentials or submission.

MJ-REQ-017.F06. Only series 2210 and seven inclusive UTC calendar dates are
requested. At most 25 announcements are enriched; this is never an inventory.
Plural control-number retrieval and constructed listing routes require separate
live qualification. A provider observation never establishes employer ATS trust.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
import threading
import time

import httpx

from services.discovery.negative_filters import parse_negative_conditions
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.search_semantics import (
    US_STATES, classify_location, normalize_employment_type,
    normalize_experience_level, normalize_title, parse_education_requirement,
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

METADATA_URL = "https://data.usajobs.gov/api/historicjoa"
ANNOUNCEMENT_TEXT_URL = "https://data.usajobs.gov/api/historicjoa/announcementtext"
ATTRIBUTION_URL = "https://www.usajobs.gov"
POSITION_SERIES = "2210"
WINDOW_DAYS = 7
MAX_SELECTED = 25
MAX_METADATA_ITEMS = 1000
MAX_RESPONSE_BYTES = 5_000_000
POLL_INTERVAL = 0.05
_CODE = re.compile(r"[A-Z0-9]{1,16}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_SERIES = re.compile(r"\d{4}\Z", re.ASCII)
# Structured USAJOBS locations can use full state names (for example Mississippi).
# Only exact names/codes with an explicit US country become a profile state code.
_US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_COLLECTIONS = (
    ("jobcategories", "jobCategories"),
    ("positionlocations", "positionLocations"),
    ("hiringpaths", "hiringPaths"),
)
_TEXT_SECTIONS = (
    ("summary", "Summary"), ("duties", "Duties"),
    ("requirementsQualifications", "Qualifications"),
    ("requirementsEducation", "Education"),
    ("requirementsConditionsOfEmployment", "Conditions of employment"),
    ("hiringPathExplanation", "Hiring eligibility"),
    ("requiredDocuments", "Required documents"), ("howToApply", "How to apply"),
)
_STATUS = {
    "accepting applications": "POSSIBLY_ACTIVE",
    "applications under review": "CLOSED",
    "reviewing applications": "CLOSED",
    "job closed": "CLOSED",
    "job canceled": "CLOSED",
    "position filled": "FILLED",
}


def _fingerprint(value):
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SourceError("SOURCE_INVALID_JOB") from None
    return hashlib.sha256(encoded).digest()


def _identifier(value, *, limit=200):
    if (not isinstance(value, str) or not value or len(value) > limit
            or value.strip() != value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise SourceError("SOURCE_INVALID_JOB")
    return value


def _date(value, *, required=False):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        if required:
            raise SourceError("SOURCE_INVALID_JOB")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        if required:
            raise SourceError("SOURCE_INVALID_JOB") from None
        return None


def _identity(record):
    if not isinstance(record, dict):
        raise SourceError("SOURCE_INVALID_JOB")
    control = record.get("usajobsControlNumber")
    agency = record.get("hiringAgencyCode")
    department = record.get("hiringDepartmentCode")
    if (type(control) is not int or not 0 < control <= 2**63 - 1
            or not isinstance(agency, str) or not _CODE.fullmatch(agency)
            or not isinstance(department, str) or not _CODE.fullmatch(department)):
        raise SourceError("SOURCE_INVALID_JOB")
    opened = _date(record.get("positionOpenDate"), required=True)
    announcement = _identifier(record.get("announcementNumber"))
    return control, agency, opened.isoformat(), announcement, department


def _alias(record, first, second, *, default=None):
    if first in record and second in record:
        if _fingerprint(record[first]) != _fingerprint(record[second]):
            raise SourceError("SOURCE_ALIAS_CONFLICT")
    return record[first] if first in record else record.get(second, default)


def _collection(record, first, second):
    value = _alias(record, first, second, default=[])
    if not isinstance(value, list) or len(value) > 100:
        raise SourceError("SOURCE_INVALID_JOB")
    if any(not isinstance(item, dict) for item in value):
        raise SourceError("SOURCE_INVALID_JOB")
    return value


def _collections(record):
    return {first: _collection(record, first, second) for first, second in _COLLECTIONS}


def _series(record):
    values = _collection(record, "jobcategories", "jobCategories")
    series = []
    for item in values:
        value = item.get("series")
        if not isinstance(value, str) or not _SERIES.fullmatch(value):
            raise SourceError("SOURCE_INVALID_JOB")
        series.append(value)
    return tuple(series)


def _text(value, *, limit=1000, required=False):
    if value is None:
        if required:
            raise SourceError("SOURCE_INVALID_JOB")
        return ""
    if not isinstance(value, str):
        raise SourceError("SOURCE_INVALID_JOB")
    cleaned = structured_text(value)[:limit].strip()
    if required and not cleaned:
        raise SourceError("SOURCE_INVALID_JOB")
    return cleaned


def _envelope(raw):
    if not isinstance(raw, dict) or set(raw) != {"metadata", "announcement_text"}:
        raise SourceError("SOURCE_INVALID_JOB")
    metadata, announcement = raw["metadata"], raw["announcement_text"]
    if _identity(metadata) != _identity(announcement):
        raise SourceError("SOURCE_JOIN_MISMATCH")
    _fingerprint(raw)
    _collections(metadata)
    _collections(announcement)
    return metadata, announcement


def company_identity(raw):
    """Use native codes and a stable explicit code label; names remain evidence."""
    metadata, _announcement = _envelope(raw)
    _control, agency, _opened, _number, department = _identity(metadata)
    return SourceCompany(
        identity_parts=("usajobs", department, agency),
        name="USAJOBS agency " + agency,
        domain=ATTRIBUTION_URL,
    )


def _invalid_constant(_value):
    raise ValueError()


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError()
    return parsed


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


class USAJobs:
    def __init__(self, tenant="default", transport=None, check=None,
                 request_timeout=10, rate_limit=60, concurrency=1, *, clock=None):
        if not isinstance(tenant, str) or tenant.strip().casefold() != "default":
            raise ValueError("SOURCE_INVALID_TENANT")
        if type(request_timeout) is not int or not 2 <= request_timeout <= 60:
            raise ValueError("SOURCE_INVALID_REQUEST_TIMEOUT")
        if type(rate_limit) is not int or not 1 <= rate_limit <= 600:
            raise ValueError("SOURCE_INVALID_RATE_LIMIT")
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("SOURCE_INVALID_CONCURRENCY")
        if clock is not None and not callable(clock):
            raise ValueError("SOURCE_INVALID_CLOCK")
        self.tenant = "default"
        self.transport = transport
        self.check = check or (lambda: None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
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
            SourceCapability.DISCOVERY, SourceCapability.APPLICATION_REDIRECT,
        }))

    def health(self):
        with self._status_lock:
            return self.status

    def _set_status(self, status):
        with self._status_lock:
            self.status = status

    def _window(self):
        instant = self.clock()
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("SOURCE_INVALID_CLOCK")
        try:
            last = instant.astimezone(timezone.utc).date()
            return last - timedelta(days=WINDOW_DAYS - 1), last
        except (ValueError, OverflowError):
            raise ValueError("SOURCE_INVALID_CLOCK") from None

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

    def _request(self, endpoint, parameters):
        if endpoint not in (METADATA_URL, ANNOUNCEMENT_TEXT_URL):
            raise SourceError("SOURCE_INVALID_ENDPOINT")
        self.check()
        while not self._request_slots.acquire(timeout=POLL_INTERVAL):
            self.check()
        try:
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
                                  "Accept": "application/json", "Accept-Encoding": "identity",
                              }) as client:
                bounded_check()
                with client.stream("GET", endpoint, params=parameters) as response:
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
                    media = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
                    encoding = response.headers.get("content-encoding", "").strip().casefold()
                    if media != "application/json" or encoding not in {"", "identity"}:
                        raise SourceError("SOURCE_PAYLOAD_INVALID")
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=None):
                        bounded_check()
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise SourceError("SOURCE_PAYLOAD_LIMIT")
                        content.extend(chunk)
                    bounded_check()
                    try:
                        payload = json.loads(content, parse_constant=_invalid_constant,
                                             parse_float=_finite_float, object_pairs_hook=_unique_object)
                    except (ValueError, UnicodeError, RecursionError):
                        bounded_check()
                        raise SourceError("SOURCE_PAYLOAD_INVALID") from None
                    bounded_check()
                    return payload
        except httpx.TimeoutException:
            self.check()
            raise SourceError("SOURCE_TIMEOUT", retry_seconds=120) from None
        except httpx.HTTPError:
            self.check()
            raise SourceError("SOURCE_NETWORK_ERROR", retry_seconds=120) from None
        finally:
            self._request_slots.release()

    @staticmethod
    def _rows(payload, limit):
        if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                or len(payload["data"]) > limit):
            raise SourceError("SOURCE_PAYLOAD_INVALID")
        if any(not isinstance(row, dict) for row in payload["data"]):
            raise SourceError("SOURCE_INVALID_JOB")
        return payload["data"]

    def search(self):
        """Two fixed reads at most; atomic enrichment or a stable source error."""
        self.check()
        first, last = self._window()
        try:
            payload = self._request(METADATA_URL, {
                "PositionSeries": POSITION_SERIES,
                "StartPositionOpenDate": first.strftime("%m-%d-%Y"),
                "EndPositionOpenDate": last.strftime("%m-%d-%Y"),
            })
            unique = {}
            signatures = {}
            eligible = []
            for row in self._rows(payload, MAX_METADATA_ITEMS):
                self.check()
                key = _identity(row)
                _collections(row)
                categories = _series(row)
                signature = _fingerprint(row)
                control = key[0]
                if control in unique:
                    if signatures[control] != signature:
                        raise SourceError("SOURCE_JOIN_CONFLICT")
                    continue
                unique[control] = row
                signatures[control] = signature
                if first <= date.fromisoformat(key[2]) <= last and POSITION_SERIES in categories:
                    eligible.append(row)
            self.check()
            eligible.sort(key=lambda row: (row["positionOpenDate"], row["usajobsControlNumber"]), reverse=True)
            selected = eligible[:MAX_SELECTED]
            selected_by_control = {row["usajobsControlNumber"]: row for row in selected}
            texts = {}
            text_signatures = {}
            if selected:
                payload = self._request(ANNOUNCEMENT_TEXT_URL, {
                    "USAJOBSControlNumbers": ",".join(str(row["usajobsControlNumber"]) for row in selected),
                })
                for row in self._rows(payload, MAX_SELECTED):
                    self.check()
                    key = _identity(row)
                    _collections(row)
                    expected = selected_by_control.get(key[0])
                    if expected is None or key != _identity(expected):
                        raise SourceError("SOURCE_JOIN_MISMATCH")
                    signature = _fingerprint(row)
                    if key[0] in texts and text_signatures[key[0]] != signature:
                        raise SourceError("SOURCE_JOIN_CONFLICT")
                    texts[key[0]] = row
                    text_signatures[key[0]] = signature
                if set(texts) != set(selected_by_control):
                    raise SourceError("SOURCE_JOIN_MISSING")
            items = []
            for row in selected:
                self.check()
                raw = {"metadata": deepcopy(row),
                       "announcement_text": deepcopy(texts[row["usajobsControlNumber"]])}
                items.append(SourceItem(raw, company_identity(raw)))
            self.check()
        except SourceError as failure:
            # A local cancellation/deadline takes precedence over provider health.
            self.check()
            self._set_status(failure.code)
            raise
        self._set_status("HEALTHY")
        return SearchBatch(endpoint=METADATA_URL, items=tuple(items), source_company=None,
                           complete_inventory=False)

    def fetch(self, job_id):
        """Search enrichment is implemented; the detail-fetch contract is not."""
        raise SourceError("SOURCE_UNSUPPORTED")

    def normalize(self, raw):
        self.check()
        metadata, announcement = _envelope(raw)
        company = company_identity(raw)
        control, agency, opened, number, department = _identity(metadata)
        title = _text(metadata.get("positionTitle"), limit=500, required=True)
        sections = []
        for field, label in _TEXT_SECTIONS:
            self.check()
            value = _text(announcement.get(field), limit=50000)
            if value:
                sections.append(label + "\n" + value)
        description = "\n\n".join(sections)[:50000]
        if not description:
            raise SourceError("SOURCE_INVALID_JOB")
        places = []
        for item in _collection(metadata, "positionlocations", "positionLocations"):
            self.check()
            place = {
                field: _text(_alias(item, lower, documented), limit=200)
                for field, lower, documented in (
                    ("city", "positionlocationcity", "positionLocationCity"),
                    ("state", "positionlocationstate", "positionLocationState"),
                    ("country", "positionlocationcountry", "positionLocationCountry"),
                )
            }
            if any(place.values()) and place not in places:
                places.append(place)
        location = "; ".join(", ".join(place[field] for field in ("city", "state", "country")
                                     if place[field]) for place in places)[:1000] or "Unknown"
        single = places[0] if len(places) == 1 else {}
        country = classify_location(single.get("country"))["country"] if single else None
        state = None
        if country == "US":
            native_state = single.get("state", "")
            code = native_state.upper()
            state = code if code in US_STATES else _US_STATE_NAMES.get(native_state.casefold())
        status = metadata.get("positionOpeningStatus")
        lifecycle = _STATUS.get(status.strip().casefold(), "UNKNOWN") if isinstance(status, str) else "UNKNOWN"
        experience = normalize_experience_level(title)
        title_info = normalize_title(title)
        education = parse_education_requirement(description)
        skills = extract_skills(title + " " + description)
        requirements = parse_job_requirements(description)
        url = ATTRIBUTION_URL + "/job/" + str(control)
        result = {
            "requisition": str(control), "title": title, "description": description,
            "skills": skills["labels"], "location": location, "remote_type": "UNKNOWN",
            "application_url": url,
            "attributes": {
                "provider": "usajobs", "tenant": self.tenant,
                "company_name": company.name,
                "agency_code": agency, "department_code": department,
                "source_agency_name": _text(metadata.get("hiringAgencyName"), limit=1000) or None,
                "source_department_name": _text(metadata.get("hiringDepartmentName"), limit=1000) or None,
                "native_posting_id": str(control), "native_job_id": str(control),
                "source_announcement_number": number,
                "source_attribution": {"name": "USAJOBS", "url": ATTRIBUTION_URL,
                                       "unofficial": True},
                "source_endpoints": [METADATA_URL, ANNOUNCEMENT_TEXT_URL],
                "provider_listing_url": url,
                "application_url_status": "CONSTRUCTED_UNVERIFIED",
                "application_url_construction": "USAJOBS_CONTROL_NUMBER",
                "route_confirmation": "NOT_RUN",
                "submission_capability": "MANUAL_ONLY", "fictional": False,
                "lifecycle_claim": lifecycle, "source_opening_status": status,
                "source_open_date": opened,
                "source_close_date": metadata.get("positionCloseDate"),
                "source_expire_date": metadata.get("positionExpireDate"),
                "source_open_date_precision": "DATE", "source_date_timezone": None,
                "source_close_date_precision": "DATE" if _date(metadata.get("positionCloseDate")) else "UNKNOWN",
                "source_expire_date_precision": "DATE" if _date(metadata.get("positionExpireDate")) else "UNKNOWN",
                # A date without a timezone is not a UTC-midnight timestamp.
                "reported_posted_at": None,
                "reported_posted_at_raw": None,
                "source_locations": places, "city": single.get("city") or None,
                "state": state, "country": country,
                "remote_regions": [], "remote_indicated": None,
                "telework_eligible_raw": metadata.get("teleworkEligible"),
                "salary_min": None, "salary_max": None, "salary_period": "UNKNOWN",
                "salary_currency": None, "salary_source": "UNKNOWN", "salary_raw": None,
                "source_salary_claim": {key: metadata.get(key) for key in (
                    "minimumSalary", "maximumSalary", "salaryType")},
                "employment_type": normalize_employment_type(_text(metadata.get("workSchedule"))),
                "experience_level": experience["level"], "experience_level_raw": experience["raw"],
                "normalized_title": title_info["normalized_title"], "title_family": title_info["title_family"],
                "skill_ids": skills["ids"], "skill_mentions": skills["mentions"],
                "skill_evidence": "description_mentions",
                "education_requirement": education["classification"], "education_requirement_raw": education["raw"],
                "experience_requirements": requirements["experience"], "certifications": requirements["certifications"],
                "clearance": requirements["clearance"], "work_authorization": requirements["work_authorization"],
                "negative_conditions": parse_negative_conditions(description, requirements["clearance"]),
                "source_position_series": list(_series(metadata)),
                "discovery_scope": {"position_series": POSITION_SERIES, "utc_calendar_days": WINDOW_DAYS,
                                    "max_selected": MAX_SELECTED, "complete_inventory": False},
            },
        }
        self.check()
        return result
