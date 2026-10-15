"""Read exact stored producer provenance without consulting current templates."""
from sqlalchemy import select

from packages.persistence import models as m
from services.ai.prompt_provenance import resolve_provenance


_TARGETS = {"resume": "resume", "cover-letter": "cover_letter",
            "application-answer-export": "screening_answers"}
_PARSER_TEMPLATES = {
    "greenhouse": "services.discovery.sources.greenhouse.Greenhouse.normalize",
    "lever": "services.discovery.sources.lever.Lever.normalize",
    "remotive": "services.discovery.sources.remotive.Remotive.normalize",
    "remoteok": "services.discovery.sources.remoteok.RemoteOK.normalize",
    "wwr": "services.discovery.sources.wwr.WeWorkRemotely.normalize",
}


def document_provenance(store, profile_id, document_id):
    """Profile-scoped readback; legacy/unrecorded output is never inferred."""
    with store.engine.connect() as connection:
        row = connection.execute(select(m.documents).join(m.applications,
            m.documents.c.application_id == m.applications.c.id).where(
                m.documents.c.id == document_id,
                m.applications.c.profile_id == profile_id)).mappings().one_or_none()
    if row is None:
        raise ValueError("PROVENANCE_UNAVAILABLE")
    context = row["generation_context"]
    if not isinstance(context, dict) or "prompt_provenance" not in context:
        raise ValueError("PROVENANCE_UNRECORDED")
    result = resolve_provenance(context["prompt_provenance"])
    if (result["target"] != _TARGETS.get(row["kind"])
            or result["template_id"] != row["template"]
            or result["model_id"] != row["generation_model"]):
        raise ValueError("PROVENANCE_BINDING_MISMATCH")
    return result


def parsing_provenance(store, candidate_id):
    """Source observations are shared public job data, not profile-owned output."""
    with store.engine.connect() as connection:
        row = connection.execute(select(m.posting_value_candidates.c.normalized,
            m.company_sources.c.provider).join(m.company_sources,
                m.posting_value_candidates.c.company_source_id == m.company_sources.c.id).where(
                    m.posting_value_candidates.c.id == candidate_id)).mappings().one_or_none()
    if row is None:
        raise ValueError("PROVENANCE_UNAVAILABLE")
    normalized = row["normalized"]
    if not isinstance(normalized, dict) or not isinstance(normalized.get("attributes"), dict):
        raise ValueError("PROVENANCE_METADATA_INVALID")
    if "prompt_provenance" not in normalized["attributes"]:
        raise ValueError("PROVENANCE_UNRECORDED")
    result = resolve_provenance(normalized["attributes"]["prompt_provenance"])
    if (result["target"] != "job_parsing"
            or result["template_id"] != _PARSER_TEMPLATES.get(row["provider"])):
        raise ValueError("PROVENANCE_BINDING_MISMATCH")
    return result
