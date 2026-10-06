"""Persist non-secret AI task routing per career profile."""
from alembic import op
import sqlalchemy as sa


revision = "7c6e1d3a9b20"
down_revision = "cf4216a93b85"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_ai_task_settings",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128)),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("profile_id", "task", name="uq_profile_ai_task_settings_profile_task"),
        sa.CheckConstraint(
            "(provider = 'disabled' AND model IS NULL) OR "
            "(provider <> 'disabled' AND model IS NOT NULL)",
            name="ck_profile_ai_task_provider_model",
        ),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM profile_ai_task_settings LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard saved AI task settings; restore a verified backup "
            "for intentional rollback"
        )
    op.drop_table("profile_ai_task_settings")
