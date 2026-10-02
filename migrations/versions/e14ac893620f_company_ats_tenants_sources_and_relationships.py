"""Persist ATS tenant metadata, independent employer sources and hierarchy.

Revision ID: e14ac893620f
Revises: cb72f9041d6a
Create Date: 2026-10-01
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e14ac893620f"
down_revision: Union[str, Sequence[str], None] = "cb72f9041d6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def upgrade() -> None:
    op.create_table(
        "company_ats_tenants",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("company_id", sa.String(length=128), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("tenant_identifier", sa.String(length=128), nullable=False),
        sa.Column("organization_identifier", sa.String(length=128), nullable=False),
        sa.Column("board_identifier", sa.String(length=128), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("company_id", "provider", "tenant_identifier",
                            "organization_identifier", "board_identifier"),
    )

    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("parent_company_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("relationship_type", sa.String(length=32), nullable=True))
        batch.create_foreign_key("fk_companies_parent_company", "companies", ["parent_company_id"], ["id"])

    additions = (
        sa.Column("ats_tenant_id", sa.String(length=128), nullable=True),
        sa.Column("career_url", sa.String(length=2000), nullable=True),
        sa.Column("search_url", sa.String(length=2000), nullable=True),
        sa.Column("feed_url", sa.String(length=2000), nullable=True),
        sa.Column("public_api_url", sa.String(length=2000), nullable=True),
        sa.Column("collection_strategy", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.45"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.String(length=32), nullable=False, server_default="NORMAL"),
        sa.Column("last_validation", sa.String(length=64), nullable=True),
        sa.Column("last_success", sa.String(length=64), nullable=True),
    )
    with op.batch_alter_table("company_job_sources") as batch:
        for column in additions:
            batch.add_column(column)
        batch.create_foreign_key("fk_company_job_sources_ats_tenant", "company_ats_tenants",
                                 ["ats_tenant_id"], ["id"])
        batch.create_unique_constraint("uq_company_job_sources_portal",
                                       ["company_id", "provider", "tenant", "career_url"])

    connection = op.get_bind()
    sources = sa.table(
        "company_job_sources",
        sa.column("id", sa.String()), sa.column("company_id", sa.String()),
        sa.column("provider", sa.String()), sa.column("tenant", sa.String()),
        sa.column("ats_tenant_id", sa.String()),
    )
    tenants = sa.table(
        "company_ats_tenants",
        sa.column("id", sa.String()), sa.column("company_id", sa.String()),
        sa.column("provider", sa.String()), sa.column("tenant_identifier", sa.String()),
        sa.column("organization_identifier", sa.String()), sa.column("board_identifier", sa.String()),
        sa.column("confidence", sa.Float()), sa.column("evidence", sa.JSON()),
        sa.column("first_seen_at", sa.String()), sa.column("last_seen_at", sa.String()),
    )
    timestamp = datetime.now(timezone.utc).isoformat()
    tenant_ids = {}
    for row in connection.execute(sa.select(
            sources.c.id, sources.c.company_id, sources.c.provider, sources.c.tenant)):
        natural_key = (row.company_id, row.provider.casefold(), row.tenant.casefold(), "", "")
        tenant_id = tenant_ids.get(natural_key)
        if tenant_id is None:
            tenant_id = _identity(*natural_key)
            tenant_ids[natural_key] = tenant_id
            connection.execute(tenants.insert().values(
                id=tenant_id, company_id=row.company_id, provider=natural_key[1],
                tenant_identifier=natural_key[2], organization_identifier="", board_identifier="",
                confidence=0.45, evidence=["legacy company source; tenant metadata not independently verified"],
                first_seen_at=timestamp, last_seen_at=timestamp,
            ))
        connection.execute(sources.update().where(sources.c.id == row.id).values(ats_tenant_id=tenant_id))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM company_ats_tenants LIMIT 1")).first():
        raise RuntimeError("Refusing to discard company ATS tenant evidence; restore a verified backup for intentional rollback")
    if connection.execute(sa.text(
            "SELECT 1 FROM companies WHERE parent_company_id IS NOT NULL OR relationship_type IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Refusing to discard company parent relationships; restore a verified backup for intentional rollback")
    with op.batch_alter_table("company_job_sources") as batch:
        batch.drop_constraint("uq_company_job_sources_portal", type_="unique")
        batch.drop_constraint("fk_company_job_sources_ats_tenant", type_="foreignkey")
        for column in (
            "last_success", "last_validation", "priority", "enabled", "confidence", "collection_strategy",
            "public_api_url", "feed_url", "search_url", "career_url", "ats_tenant_id",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("companies") as batch:
        batch.drop_constraint("fk_companies_parent_company", type_="foreignkey")
        batch.drop_column("relationship_type")
        batch.drop_column("parent_company_id")
    op.drop_table("company_ats_tenants")
