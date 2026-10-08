"""User-reviewed employer context bound to existing verified company sources.

This module never fetches URLs. User-entered statements remain explicitly
USER_PROVIDED; only source identity is checked against persisted domain records.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import math
import re
from urllib.parse import urlsplit

from sqlalchemy import func, select

from packages.persistence import models as m
from packages.persistence.store import Store, identity, now, upsert
from services.company_discovery.domains import VERIFIED_METHODS, normalize_domain


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _source_url(value):
    raw = str(value or "")
    if not raw or raw != raw.strip() or len(raw) > 2000:
        raise ValueError("COMPANY_CONTEXT_SOURCE_URL_INVALID")
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("COMPANY_CONTEXT_SOURCE_URL_INVALID")
        parsed.port
    except ValueError as error:
        raise ValueError("COMPANY_CONTEXT_SOURCE_URL_INVALID") from error
    host = normalize_domain(parsed.hostname)
    if not host:
        raise ValueError("COMPANY_CONTEXT_SOURCE_URL_INVALID")
    return raw, host


def _within_domain(host, domain):
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _verified_source_identity(connection, source_id, company_id=None):
    source = connection.execute(select(m.company_sources).where(
        m.company_sources.c.id == source_id)).mappings().one_or_none()
    if source is None or (company_id is not None and source["company_id"] != company_id):
        return None
    career_url = source.get("career_url")
    if not career_url:
        return None
    try:
        _, career_host = _source_url(career_url)
    except ValueError:
        return None
    domains = list(connection.execute(select(m.company_domains).where(
        m.company_domains.c.company_id == source["company_id"],
        m.company_domains.c.verified.is_(True),
        m.company_domains.c.verification_method.in_(VERIFIED_METHODS),
    ).order_by(m.company_domains.c.confidence.desc(), m.company_domains.c.domain)).mappings())
    if not domains:
        return None
    winner = domains[0]
    try:
        confidence = [float(row["confidence"]) for row in domains]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in confidence):
        return None
    if len(domains) > 1 and not confidence[0] > confidence[1]:
        return None
    domain = normalize_domain(winner["domain"])
    if not domain or not _within_domain(career_host, domain):
        return None
    return {
        "company_id": source["company_id"],
        "company_source_id": source["id"],
        "verified_domain": domain,
        "source_identity_method": winner["verification_method"],
    }


def _identity_for_source_url(connection, source_id, source_url, company_id=None):
    identity_data = _verified_source_identity(connection, source_id, company_id)
    if identity_data is None:
        return None
    try:
        _, source_host = _source_url(source_url)
    except ValueError:
        return None
    if not _within_domain(source_host, identity_data["verified_domain"]):
        return None
    return {**identity_data, "source_host": source_host}


def _public(row):
    result = dict(row)
    result["user_reviewed"] = bool(result["user_reviewed"])
    result["user_approved"] = bool(result["user_approved"])
    integrity = hashlib.sha256(result["content"].encode("utf-8")).hexdigest() == result["content_sha256"]
    result["content_integrity"] = "VALID" if integrity else "MISMATCH"
    if not integrity:
        result["user_approved"] = False
    return result


def _display(connection, row):
    item = _public(row)
    proof = _identity_for_source_url(connection, row["company_source_id"],
                                     row["source_url"], row["company_id"])
    current = bool(proof and row["identity_status"] == "VERIFIED"
                   and row["verified_domain"] == proof["verified_domain"]
                   and row["source_identity_method"] == proof["source_identity_method"]
                   and row["source_host"] == proof["source_host"])
    item["identity_status_recorded"] = row["identity_status"]
    item["identity_status"] = "VERIFIED" if current else "UNVERIFIED"
    if not current or item["content_integrity"] != "VALID":
        item["user_approved"] = False
    return item


def save_company_context(store, source_id, *, text, source_url, request_id):
    """Persist a new immutable user-supplied context version without fetching."""
    source_id = str(source_id or "").strip()
    content = str(text or "").strip()
    if not source_id:
        raise ValueError("COMPANY_CONTEXT_SOURCE_REQUIRED")
    if not content or len(content) > 4000:
        raise ValueError("COMPANY_CONTEXT_TEXT_INVALID")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("COMPANY_CONTEXT_REQUEST_ID_INVALID")
    source_url, source_host = _source_url(source_url)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    timestamp = now()
    context_id = identity("company-context", source_id, request_id)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        source = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id).with_for_update()).mappings().one_or_none()
        if source is None:
            raise ValueError("COMPANY_CONTEXT_SOURCE_NOT_FOUND")
        prior = connection.execute(select(m.company_contexts).where(
            m.company_contexts.c.company_source_id == source_id,
            m.company_contexts.c.request_id == request_id).with_for_update()).mappings().one_or_none()
        if prior:
            if prior["content"] != content or prior["source_url"] != source_url:
                raise ValueError("COMPANY_CONTEXT_REQUEST_CONFLICT")
            return _display(connection, prior)
        proof = _identity_for_source_url(connection, source_id, source_url, source["company_id"])
        version = int(connection.execute(select(func.coalesce(
            func.max(m.company_contexts.c.version), 0)).where(
                m.company_contexts.c.company_source_id == source_id)).scalar_one()) + 1
        row = {
            "id": context_id,
            "company_id": source["company_id"],
            "company_source_id": source_id,
            "version": version,
            "request_id": request_id,
            "content": content,
            "content_origin": "USER_PROVIDED",
            "content_status": "UNVERIFIED",
            "content_sha256": digest,
            "source_url": source_url,
            "source_host": source_host,
            "verified_domain": proof["verified_domain"] if proof else None,
            "source_identity_method": proof["source_identity_method"] if proof else None,
            "identity_status": "VERIFIED" if proof else "UNVERIFIED",
            "user_reviewed": False,
            "user_approved": False,
            "reviewed_at": None,
            "created_at": timestamp,
        }
        upsert(connection, m.company_contexts, row)
        store._audit(connection, context_id, "COMPANY_CONTEXT_SAVED", {
            "company_id": source["company_id"], "company_source_id": source_id,
            "version": version, "content_origin": "USER_PROVIDED",
            "content_sha256": digest, "identity_status": row["identity_status"],
        })
        return _display(connection, row)


def review_company_context(store, context_id, *, approved, content_sha256,
                           company_source_id=None):
    """Record explicit review after fresh identity and exact-content checks."""
    if type(approved) is not bool:
        raise ValueError("COMPANY_CONTEXT_REVIEW_INVALID")
    if not isinstance(content_sha256, str) or not _SHA256.fullmatch(content_sha256):
        raise ValueError("COMPANY_CONTEXT_CONTENT_CHANGED")
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        query = select(m.company_contexts).where(m.company_contexts.c.id == context_id)
        if company_source_id is not None:
            query = query.where(m.company_contexts.c.company_source_id == company_source_id)
        row = connection.execute(query.with_for_update()).mappings().one_or_none()
        if row is None:
            raise ValueError("COMPANY_CONTEXT_NOT_FOUND")
        actual_hash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
        if actual_hash != row["content_sha256"] or content_sha256 != actual_hash:
            raise ValueError("COMPANY_CONTEXT_CONTENT_CHANGED")
        proof = _identity_for_source_url(connection, row["company_source_id"],
                                         row["source_url"], row["company_id"])
        if approved and (proof is None or row["identity_status"] != "VERIFIED"
                         or row["verified_domain"] != proof["verified_domain"]
                         or row["source_identity_method"] != proof["source_identity_method"]
                         or row["source_host"] != proof["source_host"]):
            raise ValueError("COMPANY_CONTEXT_SOURCE_UNVERIFIED")
        timestamp = now()
        connection.execute(m.company_contexts.update().where(
            m.company_contexts.c.id == context_id).values(
                user_reviewed=True, user_approved=approved, reviewed_at=timestamp))
        store._audit(connection, context_id, "COMPANY_CONTEXT_REVIEWED", {
            "company_source_id": row["company_source_id"], "version": row["version"],
            "approved": approved, "content_sha256": actual_hash,
            "identity_status": row["identity_status"],
        })
        current = connection.execute(select(m.company_contexts).where(
            m.company_contexts.c.id == context_id)).mappings().one()
        return _display(connection, current)


def list_company_contexts(store, company_source_id):
    """Return the saved versions for the exact source, newest first."""
    with store.engine.connect() as connection:
        rows = connection.execute(select(m.company_contexts).where(
            m.company_contexts.c.company_source_id == company_source_id).order_by(
                m.company_contexts.c.version.desc())).mappings()
        return [_display(connection, row) for row in rows]


def _connection(store_or_connection):
    if isinstance(store_or_connection, Store):
        return store_or_connection.engine.connect()
    return nullcontext(store_or_connection)


def approved_company_context_for_job(store_or_connection, job_id, company_id):
    """Return the newest approved context for the one exact job-feed source."""
    with _connection(store_or_connection) as connection:
        posting_sources = set(connection.execute(select(m.postings.c.source).where(
            m.postings.c.job_id == job_id,
            m.postings.c.source.like("company-feed:%"))).scalars())
        source_ids = {value.removeprefix("company-feed:") for value in posting_sources
                      if value.startswith("company-feed:") and value.removeprefix("company-feed:")}
        if len(source_ids) != 1:
            return None
        source_id = next(iter(source_ids))
        source = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id,
            m.company_sources.c.company_id == company_id)).mappings().one_or_none()
        if source is None:
            return None
        row = connection.execute(select(m.company_contexts).where(
            m.company_contexts.c.company_source_id == source_id).order_by(
                m.company_contexts.c.version.desc()).limit(1)).mappings().one_or_none()
        if (row is None or not row["user_reviewed"] or not row["user_approved"]
                or row["identity_status"] != "VERIFIED" or row["content_origin"] != "USER_PROVIDED"
                or row["content_status"] != "UNVERIFIED"
                or hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != row["content_sha256"]):
            return None
        proof = _identity_for_source_url(connection, source_id, row["source_url"], company_id)
        if (proof is None or row["company_id"] != company_id
                or row["verified_domain"] != proof["verified_domain"]
                or row["source_identity_method"] != proof["source_identity_method"]
                or row["source_host"] != proof["source_host"]):
            return None
        return _public(row)
