"""Persist bounded AI-work policies for each career profile."""
from alembic import op
import sqlalchemy as sa


revision = "8f3a1c6d92b4"
down_revision = "7c6e1d3a9b20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_ai_cost_policies",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("minimum_score", sa.Integer(), nullable=False),
        sa.Column("max_input_candidates", sa.Integer(), nullable=False),
        sa.Column("max_ai_candidates", sa.Integer(), nullable=False),
        sa.Column("max_document_candidates", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("profile_id", name="uq_profile_ai_cost_policy_profile"),
        sa.CheckConstraint(
            "minimum_score >= 0 AND minimum_score <= 100 AND "
            "max_input_candidates >= 1 AND max_input_candidates <= 100000 AND "
            "max_ai_candidates >= 0 AND max_ai_candidates <= 75 AND "
            "max_document_candidates >= 0 AND max_document_candidates <= 20 AND "
            "max_document_candidates <= max_ai_candidates",
            name="ck_profile_ai_cost_policy_bounds",
        ),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM profile_ai_cost_policies LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard saved AI cost policies; restore a verified backup "
            "for intentional rollback"
        )
    op.drop_table("profile_ai_cost_policies")
