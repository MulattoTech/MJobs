"""Shared profile visibility for persisted operation lists and queue counts."""
from sqlalchemy import and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from packages.persistence import models as m


def _refresh_profile_membership(profile_id: str, dialect_name: str):
    """Match an exact owner only in a well-formed persisted string array."""
    if dialect_name == "postgresql":
        profile_ids = cast(m.operations.c.params, JSONB)["profile_ids"]
        is_array = func.jsonb_typeof(profile_ids) == "array"
        safe_array = case(
            (is_array, profile_ids), else_=cast(literal("[]"), JSONB),
        )
        members = func.jsonb_array_elements(safe_array).table_valued("value").alias()
        is_string = func.jsonb_typeof(members.c.value) == "string"
        # JSONB containment compares JSON strings exactly, without coercing
        # numeric members or searching the serialized JSON representation.
        contains_profile = safe_array.contains([profile_id])
    elif dialect_name == "sqlite":
        is_array = func.json_type(m.operations.c.params, "$.profile_ids") == "array"
        profile_ids = func.json_extract(m.operations.c.params, "$.profile_ids")
        safe_array = case((is_array, profile_ids), else_=literal("[]"))
        members = func.json_each(safe_array).table_valued("value", "type").alias()
        is_string = members.c.type == "text"
        contains_profile = select(literal(1)).select_from(members).where(
            is_string, members.c.value == profile_id,
        ).correlate(m.operations).exists()
    else:
        raise ValueError("Operation profile visibility requires SQLite or PostgreSQL")

    # Guard expansion inside CASE: SQL evaluation order cannot make a separate
    # WHERE type check safe for scalar/object/null ownership metadata.
    invalid_member = select(literal(1)).select_from(members).where(
        ~is_string,
    ).correlate(m.operations).exists()
    return and_(is_array, contains_profile, ~invalid_member)


def operation_profile_scope(profile_id: str, *, dialect_name: str):
    """Return a SQL predicate applied before list limits and queue counts.

    Refresh operations are shared by their persisted owner array, not by their
    source identity. Other kinds retain the existing profile/application/upload
    and company-discovery relationships. This predicate never changes state or
    grants permission to retry, discard, or submit an operation.
    """
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("An operation profile identifier is required")
    app_ids = select(m.applications.c.id).where(m.applications.c.profile_id == profile_id)
    resume_ids = select(m.uploaded_resumes.c.id).where(m.uploaded_resumes.c.profile_id == profile_id)
    legacy_scope = or_(
        m.operations.c.entity_id == profile_id,
        m.operations.c.entity_id.in_(app_ids),
        m.operations.c.entity_id.in_(resume_ids),
        and_(m.operations.c.kind == "COMPANY_DISCOVER",
             m.operations.c.params["profile_id"].as_string() == profile_id),
    )
    return or_(
        and_(m.operations.c.kind != "COMPANY_SOURCE_REFRESH", legacy_scope),
        and_(m.operations.c.kind == "COMPANY_SOURCE_REFRESH",
             _refresh_profile_membership(profile_id, dialect_name)),
    )
