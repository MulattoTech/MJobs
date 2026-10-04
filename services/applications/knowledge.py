"""Reuse persisted ATS identity without fetching, detection, or execution authority."""
from sqlalchemy import select

from packages.persistence import models as m
from services.ats_detection.detector import KNOWN_ATS_PROVIDERS


def application_adapter_knowledge(connection, route_id, *, operation_mode=None):
    """Resolve a hint for this exact route; ambiguity never selects an adapter.

    Company-only knowledge is advisory: a company's ATS does not establish that
    an aggregator or feed application URL belongs to that ATS. No URL is rewritten
    and no provider string is used as an import path or execution instruction.
    """
    route = connection.execute(select(m.routes).where(m.routes.c.id == route_id)).mappings().one()
    known_providers = KNOWN_ATS_PROVIDERS | {"mock-ats"}
    rows = connection.execute(select(m.company_ats_tenants).where(
        m.company_ats_tenants.c.company_id == route["company_id"]
    ).order_by(m.company_ats_tenants.c.id)).mappings()
    candidates = [{key: row[key] for key in (
        "id", "provider", "tenant_identifier", "organization_identifier",
        "board_identifier", "confidence", "last_seen_at",
    )} for row in rows if row["provider"] in known_providers
        and isinstance(row["evidence"], list)
        and any(isinstance(item, str) and item.strip() for item in row["evidence"])]
    provider = route["provider"] if route["provider"] in known_providers else None
    matching = [item for item in candidates if item["provider"] == provider
                and item["tenant_identifier"] == route["tenant"]] if provider else candidates
    # Match the company-source review threshold; a low-confidence observation
    # remains visible for review and must not become established ATS knowledge.
    selected = (matching[0] if len(matching) == 1
                and 0.75 <= matching[0]["confidence"] <= 1.0 else None)
    basis = "ROUTE_AND_DISCOVERY" if provider and selected else "ROUTE" if provider else (
        "COMPANY_DISCOVERY" if selected else "AMBIGUOUS" if len(matching) > 1
        else "INSUFFICIENT_EVIDENCE" if matching else "UNKNOWN")
    provider = provider or (selected["provider"] if selected else None)
    bound_mode = connection.execute(select(m.workspace_identity.c.operation_mode).where(
        m.workspace_identity.c.id == "workspace")).scalar_one_or_none()
    fixture_adapter = (route["provider"] == "mock-ats" and bound_mode == "FIXTURE"
                       and operation_mode in (None, "FIXTURE"))
    return {
        "route_id": route["id"], "company_id": route["company_id"], "basis": basis,
        "provider": provider, "ats_tenant_id": selected["id"] if selected else None,
        "tenant_identifier": selected["tenant_identifier"] if selected else (
            route["tenant"] if basis == "ROUTE" else None),
        "candidates": matching,
        "adapter_key": "mock-ats" if fixture_adapter else None,
        "adapter_status": "FIXTURE_ONLY" if fixture_adapter else (
            "UNSUPPORTED" if provider else "REVIEW_REQUIRED" if matching else "UNKNOWN"),
        "external_submission_enabled": False,
    }
