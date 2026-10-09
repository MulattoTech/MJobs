"""Deterministic, evidence-backed job quality warnings from local job data only."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from html import unescape
import ipaddress
import math
import re
from urllib.parse import urlsplit


MAX_DESCRIPTION_CHARS = 20_000
MAX_EVIDENCE_CHARS = 320
MAX_COMPARISON_JOBS = 500
MAX_ANNUAL_SALARY_USD = 5_000_000
MIN_REUSED_DESCRIPTION_CHARS = 160

_TAG = re.compile(r"<[^>]{0,1000}>")
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_PAYMENT_REQUEST = re.compile(
    r"\b(?:application|equipment|training|background[ -]check|processing|onboarding)\s+(?:fee|payment|charge)\b"
    r"|\b(?:pay|send|wire|transfer|deposit)\s+(?:a\s+|an\s+|the\s+)?(?:fee|payment|deposit|money|funds)\b",
    re.I,
)
_PAYMENT_NEGATION = re.compile(
    r"(?i)\b(?:never|not|no|without|don't|do not|doesn't|does not)\b"
    r"(?:.{0,36})\b(?:ask|charge|require|pay|payment|fee|deposit)\b"
)
_MESSAGING_APP = r"(?:telegram|whatsapp|signal|wechat|wire|line|kik|viber|discord|snapchat)"
_INTERVIEW_ONLY = re.compile(
    rf"\b(?:interviews?|interviewing|screening)\b.{{0,100}}?\b(?:only|exclusively|solely|must use)\b"
    rf".{{0,60}}?\b{_MESSAGING_APP}\b"
    rf"|\b(?:interviews?|interviewing|screening)\b.{{0,60}}?\b{_MESSAGING_APP}\b"
    rf".{{0,40}}?\b(?:only|exclusively|solely)\b"
    rf"|\b(?:only|exclusively|solely)\b.{{0,60}}?\b{_MESSAGING_APP}\b.{{0,60}}?\b(?:interviews?|screening)\b",
    re.I,
)
_SHORTENER_DOMAINS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "ow.ly", "buff.ly",
    "cutt.ly", "shorturl.at", "rebrand.ly",
}
_VERIFIED_IDENTITY_VALUES = {
    "VERIFIED", "SOURCE_VERIFIED", "VERIFIED_DOMAIN", "INDEPENDENTLY_VERIFIED",
}
_ANNUALIZATION = {
    "YEAR": 1, "ANNUAL": 1, "MONTH": 12, "MONTHLY": 12,
    "WEEK": 52, "WEEKLY": 52, "DAY": 260, "DAILY": 260,
    "HOUR": 2080, "HOURLY": 2080,
}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value[:MAX_DESCRIPTION_CHARS]
    for _ in range(3):
        decoded = unescape(value)
        if decoded == value:
            break
        value = decoded
    value = _SCRIPT_STYLE.sub(" ", value)
    value = _TAG.sub(" ", value)
    value = " ".join(value.split())
    return value or None


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _field_provenance(job: Mapping, field: str) -> dict:
    attributes = _mapping(job.get("attributes"))
    all_provenance = _mapping(job.get("field_provenance"))
    if not all_provenance:
        all_provenance = _mapping(attributes.get("field_provenance"))
    item = _mapping(all_provenance.get(field))
    source = item.get("source")
    confidence = item.get("confidence")
    if not isinstance(source, str) or not source.strip() or len(source) > 80:
        source = "UNKNOWN"
    else:
        source = source.strip()
    if (type(confidence) not in {int, float} or not 0 <= confidence <= 1
            or (type(confidence) is float and not math.isfinite(confidence))):
        confidence = None
    elif type(confidence) is float:
        confidence = float(confidence)
    return {"source": source, "confidence": confidence}


def _evidence(job: Mapping, field: str, evidence_text: object, **extra) -> dict:
    text = _text(evidence_text) or "UNKNOWN"
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS - 1].rstrip() + "…"
    return {
        "field": field,
        "text": text,
        **_field_provenance(job, field),
        **extra,
    }


def _signal(code: str, state: str, *, reason: str | None = None,
            warning: dict | None = None) -> dict:
    signal = {"code": code, "state": state}
    if reason:
        signal["reason"] = reason
    if warning is not None:
        signal["warning"] = warning
    return signal


def _warning(code: str, message: str, evidence: dict) -> dict:
    return {
        "code": code,
        "level": "ADVISORY",
        "message": message,
        "evidence": evidence,
    }


def _format_amount(value: object) -> str:
    if type(value) is int:
        return f"{value:,}"
    if type(value) is float and math.isfinite(value):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return "UNKNOWN"


def _valid_amount(value: object) -> bool:
    return (type(value) is int and value >= 0) or (
        type(value) is float and math.isfinite(value) and value >= 0)


def _salary_signal(job: Mapping) -> dict:
    attributes = _mapping(job.get("attributes"))
    minimum = attributes.get("salary_min", job.get("salary_min"))
    maximum = attributes.get("salary_max", job.get("salary_max"))
    period = attributes.get("salary_period", job.get("salary_period"))
    currency = attributes.get("salary_currency", job.get("salary_currency"))
    if (type(currency) is not str or currency.strip().upper() != "USD"
            or type(period) is not str or period.strip().upper() not in _ANNUALIZATION):
        return _signal("UNREALISTIC_SALARY", "UNKNOWN", reason="SALARY_CURRENCY_OR_PERIOD_UNKNOWN")
    values = [value for value in (minimum, maximum) if _valid_amount(value)]
    if not values or (minimum is not None and maximum is not None
                      and (not _valid_amount(minimum)
                           or not _valid_amount(maximum) or minimum > maximum)):
        return _signal("UNREALISTIC_SALARY", "UNKNOWN", reason="SALARY_VALUE_UNKNOWN")
    annualized = max(values) * _ANNUALIZATION[period.strip().upper()]
    field = "salary_max" if maximum is not None else "salary_min"
    if annualized > MAX_ANNUAL_SALARY_USD:
        evidence = _evidence(
            job, field,
            f"{_format_amount(minimum)}–{_format_amount(maximum)} "
            f"USD per {period.strip().casefold()}",
        )
        return _signal(
            "UNREALISTIC_SALARY", "WARN",
            warning=_warning(
                "UNREALISTIC_SALARY",
                "The stated salary is unusually high for its reported period and may warrant verification.",
                evidence,
            ),
        )
    return _signal("UNREALISTIC_SALARY", "CLEAR")


def _text_signal(job: Mapping, code: str, pattern: re.Pattern, message: str,
                 *, negation: re.Pattern | None = None) -> dict:
    description = _text(job.get("description"))
    if not description:
        return _signal(code, "UNKNOWN", reason="DESCRIPTION_UNKNOWN")
    matches = list(pattern.finditer(description))
    if not matches:
        return _signal(code, "CLEAR")
    match = None
    for candidate in matches:
        if negation:
            sentence_start = max(
                description.rfind(mark, 0, candidate.start())
                for mark in (".", "!", "?", ";", "\n")
            )
            context = description[sentence_start + 1:candidate.start()]
            if negation.search(context):
                continue
        match = candidate
        break
    if match is None:
        return _signal(code, "CLEAR")
    start = max(0, match.start() - 70)
    end = min(len(description), match.end() + 70)
    evidence = _evidence(job, "description", description[start:end])
    return _signal(code, "WARN", warning=_warning(code, message, evidence))


def _domain_signal(job: Mapping) -> dict:
    field = "application_url"
    url = job.get(field)
    if not isinstance(url, str) or not url.strip():
        field = "listing_url"
        url = job.get(field)
    if not isinstance(url, str) or not url.strip():
        return _signal("SUSPICIOUS_DOMAIN", "UNKNOWN", reason="APPLICATION_URL_UNKNOWN")
    if len(url) > 2000:
        return _signal("SUSPICIOUS_DOMAIN", "UNKNOWN", reason="APPLICATION_URL_OVERSIZED")
    try:
        parsed = urlsplit(url.strip())
        hostname = parsed.hostname
        port = parsed.port
        host = hostname.encode("idna").decode("ascii").casefold().rstrip(".") if hostname else ""
    except (UnicodeError, ValueError):
        return _signal("SUSPICIOUS_DOMAIN", "UNKNOWN", reason="APPLICATION_URL_INVALID")
    if not host:
        return _signal("SUSPICIOUS_DOMAIN", "UNKNOWN", reason="APPLICATION_DOMAIN_UNKNOWN")
    reason = None
    if parsed.username is not None or parsed.password is not None:
        reason = "APPLICATION_URL_CONTAINS_CREDENTIALS"
    else:
        try:
            ipaddress.ip_address(host)
            reason = "APPLICATION_DOMAIN_IS_IP_ADDRESS"
        except ValueError:
            if any(label.startswith("xn--") for label in host.split(".")):
                reason = "APPLICATION_DOMAIN_USES_PUNYCODE"
            elif any(host == domain or host.endswith("." + domain)
                     for domain in _SHORTENER_DOMAINS):
                reason = "APPLICATION_DOMAIN_IS_URL_SHORTENER"
            elif parsed.scheme.casefold() != "https":
                reason = "APPLICATION_URL_NOT_HTTPS"
    if reason is None:
        return _signal("SUSPICIOUS_DOMAIN", "CLEAR")
    evidence = _evidence(job, field, host, reason=reason,
                         port=port if port not in {80, 443} else None)
    return _signal(
        "SUSPICIOUS_DOMAIN", "WARN",
        warning=_warning(
            "SUSPICIOUS_DOMAIN",
            "The supplied application domain has a characteristic that may warrant checking.",
            evidence,
        ),
    )


def _employer_signal(job: Mapping) -> dict:
    attributes = _mapping(job.get("attributes"))
    identity_value = job.get("company_identity", attributes.get("company_identity"))
    normalized = identity_value.strip()[:80].upper() if isinstance(identity_value, str) else "UNKNOWN"
    if normalized in _VERIFIED_IDENTITY_VALUES:
        return _signal("EMPLOYER_IDENTITY_UNKNOWN", "CLEAR")
    evidence_text = normalized if normalized not in {"", "UNKNOWN"} else "UNKNOWN"
    evidence = _evidence(job, "company_identity", evidence_text)
    return _signal(
        "EMPLOYER_IDENTITY_UNKNOWN", "WARN",
        warning=_warning(
            "EMPLOYER_IDENTITY_UNKNOWN",
            "The employer identity is not verified from the supplied job evidence.", evidence,
        ),
    )


def _normalized_description(value: object) -> str | None:
    text = _text(value)
    if not text:
        return None
    normalized = " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
    return normalized if len(normalized) >= MIN_REUSED_DESCRIPTION_CHARS else None


def _employer_identity(job: Mapping) -> tuple[str | None, str | None, str | None]:
    name = _text(job.get("company"))
    name_key = " ".join(re.findall(r"[a-z0-9]+", name.casefold())) if name else None
    company_id = job.get("company_id")
    company_id = company_id.strip() if isinstance(company_id, str) and company_id.strip() else None
    domain = job.get("company_domain")
    if not isinstance(domain, str):
        domain = _mapping(job.get("attributes")).get("company_domain")
    if isinstance(domain, str):
        domain = domain.casefold().removeprefix("www.").rstrip(".") or None
    else:
        domain = None
    return name_key, company_id, domain


def _unrelated_employers(left: Mapping, right: Mapping) -> bool:
    left_name, left_id, left_domain = _employer_identity(left)
    right_name, right_id, right_domain = _employer_identity(right)
    if not left_name or not right_name or left_name == right_name:
        return False
    if left_domain and right_domain and left_domain == right_domain:
        return False
    if left_id and right_id and left_id == right_id:
        return False
    return bool((left_id and right_id and left_id != right_id)
                or (left_domain and right_domain and left_domain != right_domain))


def _reused_text_signal(job: Mapping, comparison_jobs: Iterable[Mapping] | None) -> dict:
    description = _normalized_description(job.get("description"))
    if not description:
        return _signal("TEXT_REUSED_ACROSS_EMPLOYERS", "UNKNOWN", reason="DESCRIPTION_TOO_SHORT_OR_UNKNOWN")
    if comparison_jobs is None:
        return _signal("TEXT_REUSED_ACROSS_EMPLOYERS", "UNKNOWN", reason="COMPARISON_SET_UNKNOWN")
    if not isinstance(comparison_jobs, Iterable) or isinstance(comparison_jobs, (str, bytes, Mapping)):
        return _signal("TEXT_REUSED_ACROSS_EMPLOYERS", "UNKNOWN", reason="COMPARISON_SET_INVALID")
    current_id = job.get("id")
    for index, other in enumerate(comparison_jobs):
        if index >= MAX_COMPARISON_JOBS:
            break
        if not isinstance(other, Mapping) or other.get("id") == current_id:
            continue
        if (_unrelated_employers(job, other)
                and _normalized_description(other.get("description")) == description):
            evidence = _evidence(
                job, "description", _text(job.get("description")),
                compared_job_id=(other.get("id")[:128]
                                 if isinstance(other.get("id"), str) else "UNKNOWN"),
                compared_employer=(_text(other.get("company")) or "UNKNOWN")[:160],
            )
            return _signal(
                "TEXT_REUSED_ACROSS_EMPLOYERS", "WARN",
                warning=_warning(
                    "TEXT_REUSED_ACROSS_EMPLOYERS",
                    "This job description matches a separate employer posting and may warrant review.",
                    evidence,
                ),
            )
    return _signal("TEXT_REUSED_ACROSS_EMPLOYERS", "CLEAR")


def evaluate_job_risk(job: Mapping, *, comparison_jobs: Iterable[Mapping] | None = None) -> dict:
    """Return advisory heuristics for supplied job data; never fetch, reject, or mutate.

    `field_provenance` is optional. When absent, evidence provenance remains
    explicitly UNKNOWN rather than being inferred from a page or URL.
    """
    if not isinstance(job, Mapping):
        raise ValueError("Job must be a mapping")
    signals = [
        _salary_signal(job),
        _text_signal(
            job, "PAYMENT_REQUEST", _PAYMENT_REQUEST,
            "The description requests a payment or fee from the applicant; verify before proceeding.",
            negation=_PAYMENT_NEGATION,
        ),
        _text_signal(
            job, "MESSAGING_APP_ONLY_INTERVIEW", _INTERVIEW_ONLY,
            "The description appears to require an interview only through a messaging app; verify the process.",
        ),
        _domain_signal(job),
        _employer_signal(job),
        _reused_text_signal(job, comparison_jobs),
    ]
    warnings = [signal["warning"] for signal in signals if signal["state"] == "WARN"]
    return {
        "label": "Potential Risk" if warnings else None,
        "warnings": warnings,
        "signals": signals,
        "signals_by_code": {signal["code"]: signal for signal in signals},
    }
