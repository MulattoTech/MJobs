"""Persist company aliases and evidence-bearing domain observations.

Revision ID: cb72f9041d6a
Revises: a82e4c19d753
Create Date: 2026-10-01
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Sequence, Union
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa


revision: str = "cb72f9041d6a"
down_revision: Union[str, Sequence[str], None] = "a82e4c19d753"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _domain(value):
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not host or parsed.port not in (None, 80, 443):
            return ""
        if "." not in host or host.endswith((".localhost", ".local", ".internal")):
            return ""
        if any(host == item or host.endswith("." + item) for item in (
                "linkedin.com", "indeed.com", "crunchbase.com", "glassdoor.com",
                "ziprecruiter.com", "monster.com", "careerbuilder.com", "simplyhired.com")):
            return ""
        return host
    except ValueError:
        return ""


def upgrade() -> None:
    op.create_table(
        "company_aliases",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("company_id", sa.String(length=128), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("normalized_alias", sa.String(length=200), nullable=False),
        sa.Column("alias_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("company_id", "normalized_alias"),
    )
    op.create_table(
        "company_domains",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("company_id", sa.String(length=128), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verification_method", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.String(length=64), nullable=False),
        sa.Column("last_verified_at", sa.String(length=64), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.UniqueConstraint("company_id", "domain"),
    )
    connection = op.get_bind()
    companies = sa.table("companies", sa.column("id", sa.String()), sa.column("domain", sa.String()))
    domains = sa.table(
        "company_domains",
        sa.column("id", sa.String()), sa.column("company_id", sa.String()),
        sa.column("domain", sa.String()), sa.column("confidence", sa.Float()),
        sa.column("verified", sa.Boolean()), sa.column("verification_method", sa.String()),
        sa.column("first_seen_at", sa.String()), sa.column("last_verified_at", sa.String()),
        sa.column("evidence", sa.JSON()),
    )
    now = datetime.now(timezone.utc).isoformat()
    for company_id, old_domain in connection.execute(sa.select(companies.c.id, companies.c.domain)):
        host = _domain(old_domain)
        if not host:
            continue
        record_id = hashlib.sha256(json.dumps(
            [company_id, host], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        connection.execute(domains.insert().values(
            id=record_id, company_id=company_id, domain=host, confidence=0.45,
            verified=False, verification_method="CANDIDATE_DOMAIN_UNVERIFIED",
            first_seen_at=now, last_verified_at=None,
            evidence=["legacy companies.domain backfill; verification unavailable"],
        ))


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("company_aliases", "company_domains"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                f"Refusing to discard {table}; restore a verified backup for intentional rollback"
            )
    op.drop_table("company_domains")
    op.drop_table("company_aliases")
