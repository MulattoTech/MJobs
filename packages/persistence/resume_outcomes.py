"""Durable tables for explicit resume-version reports and outcome observations."""

from sqlalchemy import (CheckConstraint, Column, ForeignKey, Index, Integer, JSON,
                        String, Table, UniqueConstraint)

from packages.persistence.models import metadata


bindings = Table(
    "resume_outcome_bindings", metadata,
    Column("id", String(128), primary_key=True),
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("application_id", String(128), nullable=False),
    Column("document_id", String(128), nullable=False),
    Column("document_version", Integer, nullable=False),
    Column("document_sha256", JSON, nullable=False),
    Column("reported_at", String(64), nullable=False),
    Column("request_id", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    UniqueConstraint("profile_id", "application_id", name="uq_resume_outcome_application"),
    CheckConstraint("document_version > 0", name="ck_resume_outcome_document_version"),
)

events = Table(
    "resume_outcome_events", metadata,
    Column("id", String(128), primary_key=True),
    Column("binding_id", ForeignKey("resume_outcome_bindings.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("observed_at", String(64), nullable=False),
    Column("request_id", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    UniqueConstraint("binding_id", "request_id", name="uq_resume_outcome_event_request"),
    CheckConstraint(
        "kind IN ('RESPONSE','SCREEN','INTERVIEW','OFFER','REJECTED','WITHDRAWN')",
        name="ck_resume_outcome_event_kind",
    ),
)
Index("ix_resume_outcome_profile_time", bindings.c.profile_id, bindings.c.reported_at)
Index("ix_resume_outcome_binding_time", events.c.binding_id, events.c.observed_at)
