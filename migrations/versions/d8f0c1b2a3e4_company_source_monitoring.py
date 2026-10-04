"""Persist authorized company-source monitoring state."""
from alembic import op
import sqlalchemy as sa


revision = "d8f0c1b2a3e4"
down_revision = "c3e7a1b50d62"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "company_source_monitoring",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("source_id", sa.String(length=128), sa.ForeignKey("company_job_sources.id"), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("authorization", sa.String(length=32), nullable=False),
        sa.Column("domain_evidence", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("poll_interval", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="IDLE", nullable=False),
        sa.Column("last_attempt", sa.String(length=64)),
        sa.Column("next_due", sa.String(length=64)),
        sa.Column("last_error", sa.String(length=128)),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("operation_id", sa.String(length=128), sa.ForeignKey("worker_operations.id")),
        sa.Column("rediscovery_operation_id", sa.String(length=128), sa.ForeignKey("worker_operations.id")),
        sa.Column("completed_operation_id", sa.String(length=128), sa.ForeignKey("worker_operations.id")),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("source_id"),
        sa.CheckConstraint(
            '"authorization" IN (\'VERIFIED_DOMAIN\', \'USER_REVIEWED\')',
            name="company_source_monitoring_authorization_valid",
        ),
        sa.CheckConstraint(
            "poll_interval BETWEEN 300 AND 604800",
            name="company_source_monitoring_poll_interval_valid",
        ),
        sa.CheckConstraint(
            "status IN ('IDLE', 'SCHEDULED', 'HEALTHY', 'FAILED', 'BLOCKED', 'REDISCOVERY_REQUIRED')",
            name="company_source_monitoring_status_valid",
        ),
    )
    op.create_index(
        "ix_company_source_monitoring_next_due",
        "company_source_monitoring",
        ["next_due"],
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM company_source_monitoring LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard company source monitoring state; restore a verified backup for intentional rollback"
        )
    op.drop_index("ix_company_source_monitoring_next_due", table_name="company_source_monitoring")
    op.drop_table("company_source_monitoring")
