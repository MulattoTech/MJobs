"""Persist explicit, cost-capped review before AI-funnel document preparation."""
from alembic import op
import sqlalchemy as sa


revision = "c90d4a12fe37"
down_revision = "a4c19e7d603b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_document_reviews",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(length=128),
                  sa.ForeignKey("ai_evaluation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("reviewed_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("run_id", "job_id", name="uq_ai_document_review_run_job"),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM ai_document_reviews LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard explicit AI document review history; "
            "restore a verified backup for intentional rollback"
        )
    op.drop_table("ai_document_reviews")
