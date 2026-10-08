"""Persist profile-scoped answer reuse confidence thresholds."""
from alembic import op
import sqlalchemy as sa


revision = "a3e19d7c4b26"
down_revision = "8e2f4c6a1b93"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_answer_confidence_policies",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("minimum_saved_answer_confidence", sa.Integer(), nullable=False),
        sa.Column("minimum_question_match_confidence", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "profile_id", name="uq_profile_answer_confidence_policy_profile"),
        sa.CheckConstraint(
            "minimum_saved_answer_confidence >= 80 AND minimum_saved_answer_confidence <= 100 AND "
            "minimum_question_match_confidence >= 0.75 AND minimum_question_match_confidence <= 1.0",
            name="ck_profile_answer_confidence_policy_bounds"),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT 1 FROM profile_answer_confidence_policies LIMIT 1"
    )).first():
        raise RuntimeError(
            "Refusing to discard saved answer confidence thresholds; restore a verified backup "
            "for intentional rollback"
        )
    op.drop_table("profile_answer_confidence_policies")
