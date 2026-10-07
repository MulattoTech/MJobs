"""Persist profile-scoped, bounded AI job evaluation history."""
from alembic import op
import sqlalchemy as sa


revision = "a4c19e7d603b"
down_revision = "8f3a1c6d92b4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_evaluation_runs",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_version", sa.String(length=64), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("task_provider", sa.String(length=64), nullable=False),
        sa.Column("task_model", sa.String(length=128), nullable=False),
        sa.Column("evaluated_candidate_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.CheckConstraint("evaluated_candidate_count >= 0",
                           name="ck_ai_evaluation_runs_candidate_count"),
    )
    op.create_index("ix_ai_evaluation_runs_profile_created", "ai_evaluation_runs",
                    ["profile_id", "created_at"])
    op.create_table(
        "ai_job_evaluations",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("run_id", sa.String(length=128),
                  sa.ForeignKey("ai_evaluation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("company", sa.String(length=512), nullable=False),
        sa.Column("deterministic_score", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("rationale", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("run_id", "job_id", name="uq_ai_job_evaluation_run_job"),
        sa.CheckConstraint("deterministic_score >= 0 AND deterministic_score <= 100",
                           name="ck_ai_job_evaluation_score"),
        sa.CheckConstraint("category IN ('STRONG', 'REVIEW', 'WEAK')",
                           name="ck_ai_job_evaluation_category"),
    )
    op.create_index("ix_ai_job_evaluations_profile_created", "ai_job_evaluations",
                    ["profile_id", "created_at"])


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM ai_job_evaluations LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard saved AI evaluations; restore a verified backup "
            "for intentional rollback"
        )
    if connection.execute(sa.text("SELECT 1 FROM ai_evaluation_runs LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard saved AI evaluation history; restore a verified backup "
            "for intentional rollback"
        )
    op.drop_index("ix_ai_job_evaluations_profile_created", table_name="ai_job_evaluations")
    op.drop_table("ai_job_evaluations")
    op.drop_index("ix_ai_evaluation_runs_profile_created", table_name="ai_evaluation_runs")
    op.drop_table("ai_evaluation_runs")
