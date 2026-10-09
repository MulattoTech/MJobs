"""Durable immutable revisions for profile/job-scoped resume workspace layouts."""
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    UniqueConstraint,
)
from packages.persistence.models import metadata


resume_workspace_revisions = Table(
    "resume_workspace_revisions",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("profile_id", ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", ForeignKey("canonical_jobs.id", ondelete="CASCADE"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("profile_version", String(64), nullable=False),
    Column("job_fingerprint", String(64), nullable=False),
    Column("template_id", String(100), nullable=False),
    Column("sections", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "profile_id", "job_id", "version",
        name="uq_resume_workspace_profile_job_version",
    ),
    CheckConstraint("version > 0", name="ck_resume_workspace_version_positive"),
)
