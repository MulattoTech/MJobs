"""Verified company-alias recording and normalization."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.normalization.jobs import normalize_company_name


ALIAS_TYPES = {"LEGAL", "BRAND", "ABBREVIATION", "FORMER", "SOURCE"}


def normalize_alias(value):
    return normalize_company_name(str(value or "").strip())[:200]


def record_company_alias(store, company_id, alias, alias_type="SOURCE", source="", evidence=None, verified=False,
                         confidence=0.55):
    name = str(alias or "").strip()
    normalized = normalize_alias(name)
    alias_type = str(alias_type or "").upper()
    if not normalized:
        raise ValueError("COMPANY_ALIAS_REQUIRED")
    if alias_type not in ALIAS_TYPES:
        raise ValueError("COMPANY_ALIAS_TYPE_INVALID")
    confidence = max(0.0, min(1.0, float(confidence)))
    if verified:
        confidence = max(confidence, 0.90)
    data = {
        "id": identity(company_id, normalized), "company_id": company_id,
        "alias": name[:200], "normalized_alias": normalized, "alias_type": alias_type,
        "confidence": confidence, "verified": bool(verified), "source": str(source or "")[:200],
        "evidence": list(evidence or []), "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with store.engine.begin() as connection:
        existing = connection.execute(select(m.company_aliases).where(
            m.company_aliases.c.company_id == company_id,
            m.company_aliases.c.normalized_alias == normalized,
        )).mappings().one_or_none()
        if existing:
            data.update({
                "id": existing["id"],
                "confidence": max(float(existing["confidence"]), confidence),
                "verified": bool(existing["verified"] or verified),
                "source": str(source or existing["source"])[:200],
                "evidence": list(dict.fromkeys(list(existing["evidence"] or []) + data["evidence"])),
                "created_at": existing["created_at"],
            })
        upsert(connection, m.company_aliases, data, replace=True)
    return data
