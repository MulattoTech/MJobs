"""Deterministic ATS identification from already-fetched public page evidence."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class _Signature:
    provider: str
    hosts: tuple[str, ...] = ()
    patterns: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ATSDetectionCandidate:
    provider: str
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ATSDetectionResult:
    status: str
    provider: str | None
    confidence: float
    visible_careers_url: str
    evidence: tuple[str, ...]
    candidates: tuple[ATSDetectionCandidate, ...]
    application_supported: bool = False


_SIGNATURES = (
    _Signature("greenhouse", ("greenhouse.io", "grnh.se"), (
        ("SCRIPT_URL", r"(?:boards|job-boards)\.greenhouse\.io/embed|grnh\.se"),
        ("PUBLIC_API", r"(?:boards|harvest)\.greenhouse\.io"),
    )),
    _Signature("lever", ("lever.co",), (
        ("PUBLIC_API", r"api\.lever\.co/v0/postings"),
        ("SCRIPT_URL", r"(?:jobs|widget)\.lever\.co"),
    )),
    _Signature("ashby", ("ashbyhq.com",), (
        ("SCRIPT_URL", r"jobs\.ashbyhq\.com|ashbyhq\.com"),
    )),
    _Signature("smartrecruiters", ("smartrecruiters.com",), (
        ("PUBLIC_API", r"api\.smartrecruiters\.com"),
    )),
    _Signature("workable", ("workable.com",), (
        ("SCRIPT_URL", r"(?:apply|jobs)\.workable\.com"),
        ("PUBLIC_API", r"workable\.com/api/(?:jobs|widget)"),
    )),
    _Signature("workday", ("myworkdayjobs.com", "workdayjobs.com"), (
        ("URL_STRUCTURE", r"/wday/cxs/|/hcmui/candidateexperience|/job/[^/]+/[^/]+_r\d+"),
    )),
    _Signature("jobvite", ("jobvite.com",), (
        ("SCRIPT_URL", r"jobs\.jobvite\.com|jobs\.jobvite\.com"),
    )),
    _Signature("bamboohr", ("bamboohr.com",), (
        ("URL_STRUCTURE", r"/careers/list(?:\.php)?|/jobs/view\.php"),
    )),
    _Signature("recruitee", ("recruitee.com",), (
        ("SCRIPT_URL", r"(?:jobs|careers)\.recruitee\.com"),
    )),
    _Signature("teamtailor", ("teamtailor.com",), (
        ("SCRIPT_URL", r"(?:career|jobs)\.teamtailor\.com"),
    )),
    _Signature("breezy", ("breezy.hr",), (
        ("SCRIPT_URL", r"breezy\.hr"),
    )),
    _Signature("icims", ("icims.com",), (
        ("URL_STRUCTURE", r"/jobs/\d+/(?:job|.*?jobid=)|[?&]jobid=\d+"),
        ("SCRIPT_URL", r"(?:static|careers-[a-z0-9-]+)\.icims\.com"),
    )),
    _Signature("oracle_taleo", ("taleo.net",), (
        ("URL_STRUCTURE", r"/careersection/|/careersection\.ftl"),
    )),
    _Signature("successfactors", ("successfactors.com", "successfactors.eu", "successfactors.de"), (
        ("URL_STRUCTURE", r"/career\?company=|/careersection/"),
    )),
    _Signature("dayforce", ("dayforcehcm.com", "dayforce.com"), (
        ("URL_STRUCTURE", r"/mydayforce/"),
    )),
    _Signature("adp_recruiting", ("jobs.adp.com", "recruiting.adp.com"), (
        ("URL_STRUCTURE", r"/careers/(?:detail|job)/"),
    )),
    _Signature("jazzhr", ("jazz.co", "jazzhr.com"), (
        ("URL_STRUCTURE", r"/careers/[^/]+/jobs/"),
    )),
    _Signature("applicantpro", ("applicantpro.com",), ()),
    _Signature("ukg", ("ultipro.com", "jobs.ukg.com"), (
        ("URL_STRUCTURE", r"/careers/job/|/careers/jobs/"),
    )),
    _Signature("paycom", ("paycomonline.net",), (
        ("URL_STRUCTURE", r"/v4/ats/web.php/jobs/"),
    )),
    _Signature("paylocity", ("paylocity.com",), (
        ("URL_STRUCTURE", r"/recruiting/"),
    )),
    _Signature("neogov", ("governmentjobs.com", "neogov.com"), (
        ("URL_STRUCTURE", r"/careerpages/"),
    )),
)

_WEIGHTS = {
    "HOSTNAME": 0.94, "REDIRECT_HOSTNAME": 0.94, "SCRIPT_URL": 0.88,
    "FORM_ACTION": 0.88, "PUBLIC_API": 0.88, "URL_STRUCTURE": 0.60,
    "EMBEDDED_CONFIGURATION": 0.82, "HTML_SIGNATURE": 0.78,
    "STRUCTURED_METADATA": 0.75,
}
# URL paths are supporting evidence only. Common `/jobs/<id>` and
# `/recruiting/` routes must not identify an ATS without another signal.
_MIN_CONFIDENCE = 0.72
_CONFIGURATION_ATTRIBUTES = {"data-ats", "data-provider", "data-ats-provider", "data-career-provider"}
_METADATA_KEYS = {"ats", "atsprovider", "provider", "boardprovider", "recruitingplatform"}


def _host(url: str) -> str:
    try:
        parts = urlsplit(str(url or ""))
        if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""
        _ = parts.port
        return parts.hostname.casefold().rstrip(".")
    except (TypeError, ValueError):
        return ""


def _matching_hosts(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _vendor_for_host(host: str) -> list[str]:
    return [signature.provider for signature in _SIGNATURES
            if any(_matching_hosts(host, domain) for domain in signature.hosts)]


def _provider_for_config_value(value: object) -> list[str]:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    aliases = {
        "greenhouse": {"greenhouse", "greenhouseio", "grnh"},
        "lever": {"lever", "leverco"},
        "ashby": {"ashby", "ashbyhq"},
        "smartrecruiters": {"smartrecruiters"},
        "workable": {"workable"},
        "workday": {"workday", "myworkdayjobs"},
        "jobvite": {"jobvite"},
        "bamboohr": {"bamboohr"},
        "recruitee": {"recruitee"},
        "teamtailor": {"teamtailor"},
        "breezy": {"breezy", "breezyhr"},
        "icims": {"icims"},
        "oracle_taleo": {"taleo", "oracletaleo"},
        "successfactors": {"successfactors"},
        "dayforce": {"dayforce"},
        "adp_recruiting": {"adp", "adprecruiting"},
        "jazzhr": {"jazzhr", "jazz"},
        "applicantpro": {"applicantpro"},
        "ukg": {"ukg", "ultipro"},
        "paycom": {"paycom"},
        "paylocity": {"paylocity"},
        "neogov": {"neogov", "governmentjobs"},
    }
    return [provider for provider, values in aliases.items() if normalized in values]


def _metadata_provider_values(value):
    found = []
    if isinstance(value, list):
        for item in value:
            found.extend(_metadata_provider_values(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]+", "", str(key).casefold())
            if normalized_key in _METADATA_KEYS and isinstance(item, str):
                found.extend(_provider_for_config_value(item))
            elif isinstance(item, (dict, list)):
                found.extend(_metadata_provider_values(item))
    return found


def detect_ats(visible_careers_url: str, html: str = "", *,
               redirect_chain=()) -> ATSDetectionResult:
    """Identify a platform from corroborating URL/page evidence, without fetching."""
    signals: dict[str, set[str]] = {}

    def add(provider: str, signal: str):
        signals.setdefault(provider, set()).add(signal)

    current_host = _host(visible_careers_url)
    current_url = str(visible_careers_url or "")
    for provider in _vendor_for_host(current_host):
        add(provider, "HOSTNAME")

    redirects = tuple(str(url) for url in (redirect_chain or ()) if _host(url))
    for url in redirects:
        host = _host(url)
        for provider in _vendor_for_host(host):
            add(provider, "REDIRECT_HOSTNAME")
        normalized_url = url.casefold()
        for signature in _SIGNATURES:
            if any(re.search(pattern, normalized_url, re.I) for signal, pattern in signature.patterns
                   if signal == "URL_STRUCTURE"):
                add(signature.provider, "URL_STRUCTURE")

    combined = "\n".join((current_url, *redirects)).casefold()
    for signature in _SIGNATURES:
        if any(re.search(pattern, combined, re.I) for signal, pattern in signature.patterns
               if signal == "URL_STRUCTURE"):
            add(signature.provider, "URL_STRUCTURE")

    soup = BeautifulSoup(str(html or "")[:2_000_000], "html.parser")
    for tag in soup.find_all("script", src=True):
        src = str(tag.get("src") or "")
        for provider in _vendor_for_host(_host(src)):
            add(provider, "SCRIPT_URL")
        for signature in _SIGNATURES:
            for signal, pattern in signature.patterns:
                if signal in {"SCRIPT_URL", "PUBLIC_API"} and re.search(pattern, src, re.I):
                    add(signature.provider, signal)

    for tag in soup.find_all("form", action=True):
        action = str(tag.get("action") or "")
        absolute_action_host = _host(action) or _host(urljoin(current_url, action))
        for provider in _vendor_for_host(absolute_action_host):
            add(provider, "FORM_ACTION")
        for signature in _SIGNATURES:
            for signal, pattern in signature.patterns:
                if signal == "PUBLIC_API" and re.search(pattern, action, re.I):
                    add(signature.provider, "FORM_ACTION")
                elif signal == "URL_STRUCTURE" and re.search(pattern, action, re.I):
                    add(signature.provider, "URL_STRUCTURE")

    page_text = str(html or "")[:2_000_000]
    for signature in _SIGNATURES:
        for signal, pattern in signature.patterns:
            if signal == "URL_STRUCTURE" and re.search(pattern, combined, re.I):
                add(signature.provider, signal)
    # Vendor names in visible prose are weak, untrusted text. Only inspect
    # structural attributes that commonly carry widget configuration.
    for tag in soup.find_all(True):
        for name, raw in tag.attrs.items():
            if name.casefold() not in _CONFIGURATION_ATTRIBUTES:
                continue
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                for provider in _provider_for_config_value(value):
                    add(provider, "EMBEDDED_CONFIGURATION")

    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        for provider in _metadata_provider_values(payload):
            add(provider, "STRUCTURED_METADATA")

    ranked = []
    for provider, evidence in signals.items():
        if not evidence:
            continue
        confidence = 1.0
        for signal in evidence:
            confidence *= 1 - _WEIGHTS.get(signal, 0.72)
        confidence = round(1 - confidence, 2)
        if confidence >= _MIN_CONFIDENCE:
            ranked.append(ATSDetectionCandidate(provider, confidence, tuple(sorted(evidence))))
    ranked.sort(key=lambda item: (-item.confidence, item.provider))
    candidates = tuple(ranked)
    if not candidates:
        return ATSDetectionResult("NO_MATCH", None, 0.0, current_url, (), ())
    if len(candidates) > 1 and candidates[0].confidence - candidates[1].confidence < 0.1:
        return ATSDetectionResult("AMBIGUOUS", None, candidates[0].confidence,
                                  current_url, (), candidates)
    best = candidates[0]
    return ATSDetectionResult("MATCH", best.provider, best.confidence, current_url,
                              best.evidence, candidates)
