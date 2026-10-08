"""Persist user-reviewed, provenance-bound company context."""
from alembic import op
import sqlalchemy as sa


revision = "c7d4a91e205f"
down_revision = "a3e19d7c4b26"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "company_contexts",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("company_id", sa.String(length=128),
                  sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("company_source_id", sa.String(length=128),
                  sa.ForeignKey("company_job_sources.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_origin", sa.String(length=32), nullable=False,
                  server_default="USER_PROVIDED"),
        sa.Column("content_status", sa.String(length=16), nullable=False,
                  server_default="UNVERIFIED"),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("source_host", sa.String(length=253), nullable=False),
        sa.Column("verified_domain", sa.String(length=253)),
        sa.Column("source_identity_method", sa.String(length=64)),
        sa.Column("identity_status", sa.String(length=16), nullable=False),
        sa.Column("user_reviewed", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("user_approved", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("reviewed_at", sa.String(length=64)),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("company_source_id", "version",
                            name="uq_company_context_source_version"),
        sa.UniqueConstraint("company_source_id", "request_id",
                            name="uq_company_context_source_request"),
        sa.CheckConstraint("content_origin = 'USER_PROVIDED'",
                           name="ck_company_context_origin"),
        sa.CheckConstraint("content_status = 'UNVERIFIED'",
                           name="ck_company_context_content_status"),
        sa.CheckConstraint("identity_status IN ('VERIFIED','UNVERIFIED')",
                           name="ck_company_context_identity_status"),
        sa.CheckConstraint(
            "user_approved = false OR (user_reviewed = true AND identity_status = 'VERIFIED')",
            name="ck_company_context_approval_gate"),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT 1 FROM company_contexts LIMIT 1"
    )).first():
        raise RuntimeError(
            "Refusing to discard saved company context; restore a verified backup "
            "for intentional rollback"
        )
    op.drop_table("company_contexts")
