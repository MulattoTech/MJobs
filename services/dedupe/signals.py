"""Explainable duplicate-candidate signals. No ambiguous record is auto-merged here."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from services.discovery.search_semantics import normalize_title
from services.normalization.jobs import normalize_company_name


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"ref", "source", "campaign", "tracking", "trk"}


def canonical_application_url(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    port = parsed.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/+", "/", parsed.path or "/")
    path = path.rstrip("/") or "/"
    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        folded = key.casefold()
        if folded in TRACKING_QUERY_KEYS or any(folded.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        kept.append((key, value))
    kept.sort()
    return urlunsplit((parsed.scheme.casefold(), netloc, path, urlencode(kept), ""))


def _set(values):
    return {str(value).strip().casefold() for value in (values or []) if str(value).strip()}


def company_identity_signals(left, right):
    """Return explicit corroborating identity signals; normalized name alone is never sufficient."""
    signals = []
    left_official, right_official = str(left.get("official_id") or ""), str(right.get("official_id") or "")
    if left_official and right_official and left_official == right_official:
        signals.append("OFFICIAL_ID")
    if _set(left.get("domains")) & _set(right.get("domains")):
        signals.append("DOMAIN")
    if _set(left.get("ats_tenants")) & _set(right.get("ats_tenants")):
        signals.append("ATS_TENANT")
    if _set(left.get("known_identity_ids")) & _set(right.get("known_identity_ids")):
        signals.append("KNOWN_RELATIONSHIP")
    left_names = {normalize_company_name(left.get("name")), *[normalize_company_name(v) for v in left.get("aliases", [])]}
    right_names = {normalize_company_name(right.get("name")), *[normalize_company_name(v) for v in right.get("aliases", [])]}
    left_names.discard("")
    right_names.discard("")
    if left_names & right_names:
        signals.append("NAME_OR_ALIAS")
    corroborated = any(signal in signals for signal in ("OFFICIAL_ID", "DOMAIN", "ATS_TENANT", "KNOWN_RELATIONSHIP"))
    return {
        "same_company": corroborated,
        "signals": signals,
        "name_overlap_only": "NAME_OR_ALIAS" in signals and not corroborated,
    }


def dedupe_title_key(value):
    info = normalize_title(str(value or ""))
    normalized = info.get("normalized_title") or str(value or "")
    return re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).strip()


def normalize_description(value):
    text = str(value or "").casefold()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return " ".join(text.split())


def description_hash(value):
    return hashlib.sha256(normalize_description(value).encode("utf-8")).hexdigest()


def _trigrams(value):
    normalized = f"  {normalize_description(value)}  "
    return {normalized[i:i+3] for i in range(max(0, len(normalized) - 2))}


def description_similarity(left, right):
    left_norm, right_norm = normalize_description(left), normalize_description(right)
    if not left_norm and not right_norm:
        return {"exact": False, "similarity": 0.0, "method": "NO_CONTENT"}
    if not left_norm or not right_norm:
        return {"exact": False, "similarity": 0.0, "method": "TRIGRAM_JACCARD"}
    if description_hash(left_norm) == description_hash(right_norm):
        return {"exact": True, "similarity": 1.0, "method": "NORMALIZED_HASH"}
    a, b = _trigrams(left_norm), _trigrams(right_norm)
    score = len(a & b) / len(a | b) if a or b else 0.0
    return {"exact": False, "similarity": round(score, 6), "method": "TRIGRAM_JACCARD"}
def strong_identity_signals(left, right):
    """Strong job-level identifiers, independent from fuzzy description similarity."""
    signals = []
    lroute = canonical_application_url(left.get("application_url"))
    rroute = canonical_application_url(right.get("application_url"))
    if lroute and rroute and lroute == rroute:
        signals.append("APPLICATION_URL")

    ltenant, rtenant = str(left.get("ats_tenant") or ""), str(right.get("ats_tenant") or "")
    lreq, rreq = str(left.get("requisition") or ""), str(right.get("requisition") or "")
    if ltenant and rtenant and lreq and rreq and (ltenant.casefold(), lreq.casefold()) == (rtenant.casefold(), rreq.casefold()):
        signals.append("ATS_TENANT_REQUISITION")

    lemp, remp = str(left.get("employer_requisition") or ""), str(right.get("employer_requisition") or "")
    if lemp and remp and lemp.casefold() == remp.casefold():
        signals.append("EMPLOYER_REQUISITION")

    left_source = {(str(x.get("source") or "").casefold(), str(x.get("source_job_id") or "").casefold())
                   for x in left.get("source_relationships", []) if x.get("source") and x.get("source_job_id")}
    right_source = {(str(x.get("source") or "").casefold(), str(x.get("source_job_id") or "").casefold())
                    for x in right.get("source_relationships", []) if x.get("source") and x.get("source_job_id")}
    if left_source & right_source:
        signals.append("SOURCE_JOB_RELATIONSHIP")
    return signals


def duplicate_candidate_evidence(left, right):
    company = company_identity_signals(left.get("company") or {}, right.get("company") or {})
    strong = strong_identity_signals(left, right)
    title_left, title_right = dedupe_title_key(left.get("title")), dedupe_title_key(right.get("title"))
    description = description_similarity(left.get("description"), right.get("description"))
    return {
        "strong_signals": strong,
        "company": company,
        "title": {
            "left": title_left,
            "right": title_right,
            "equal": bool(title_left and title_right and title_left == title_right),
        },
        "description": description,
        "strong_match": bool(strong) and company["same_company"],
    }
