"""Persist profile-scoped, provenance-bound career story revisions."""
from alembic import op
import sqlalchemy as sa


revision = "d0c97315c313"
down_revision = "c7d4a91e205f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "career_stories",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("story_key", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("fact_ids", sa.JSON(), nullable=False),
        sa.Column("fact_fingerprints", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("allowed_uses", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("review_note", sa.String(length=2000)),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.String(length=64)),
        sa.UniqueConstraint(
            "profile_id", "story_key", "revision",
            name="uq_career_story_profile_key_revision",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_career_story_revision_positive"),
        sa.CheckConstraint(
            "kind IN ('STAR_STORY', 'ACCOMPLISHMENT', 'RESUME_BULLET')",
            name="ck_career_story_kind",
        ),
        sa.CheckConstraint(
            "length(trim(title)) >= 1 AND length(title) <= 200",
            name="ck_career_story_title_length",
        ),
        sa.CheckConstraint(
            "length(trim(text)) >= 1 AND length(text) <= 8000",
            name="ck_career_story_text_length",
        ),
        sa.CheckConstraint(
            "review_status IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="ck_career_story_review_status",
        ),
        sa.CheckConstraint(
            "review_note IS NULL OR length(review_note) <= 2000",
            name="ck_career_story_review_note_length",
        ),
        sa.CheckConstraint(
            "(review_status = 'PENDING' AND reviewed_at IS NULL) OR "
            "(review_status IN ('APPROVED', 'REJECTED') AND reviewed_at IS NOT NULL)",
            name="ck_career_story_review_timestamp",
        ),
    )
    op.create_index(
        "ix_career_stories_profile_created",
        "career_stories",
        ["profile_id", "created_at"],
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM career_stories LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard career story revisions; "
            "restore a verified backup for intentional rollback"
        )
    op.drop_index("ix_career_stories_profile_created", table_name="career_stories")
    op.drop_table("career_stories")
