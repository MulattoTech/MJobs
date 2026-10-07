"""Persist profile-scoped encrypted provider credentials."""
from alembic import op
import sqlalchemy as sa


revision = "e5019c4a7b20"
down_revision = "c90d4a12fe37"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.Column("last_tested_at", sa.String(length=64)),
        sa.Column("last_test_status", sa.String(length=24), server_default="NOT_TESTED", nullable=False),
        sa.Column("rotation_count", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("profile_id", "provider", name="uq_provider_credentials_profile_provider"),
        sa.CheckConstraint("provider IN ('openai', 'anthropic')", name="ck_provider_credentials_provider"),
        sa.CheckConstraint("last_test_status IN ('NOT_TESTED', 'CONNECTED', 'FAILED')",
                           name="ck_provider_credentials_test_status"),
        sa.CheckConstraint("rotation_count >= 0", name="ck_provider_credentials_rotation_count"),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM provider_credentials LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard provider credentials; preserve a verified database backup "
            "before intentional rollback"
        )
    op.drop_table("provider_credentials")
