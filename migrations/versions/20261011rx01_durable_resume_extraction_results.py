"""Persist profile-scoped results for queued local resume extraction."""
from alembic import op
import sqlalchemy as sa


revision = "20261011rx01"
down_revision = "20261011rt01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resume_extraction_results",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("resume_id", sa.String(length=128), nullable=False),
        sa.Column("resume_sha256", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["id"], ["worker_operations.id"], ondelete="CASCADE",
                                name="fk_resume_extraction_results_operation"),
        sa.ForeignKeyConstraint(["profile_id"], ["career_profiles.id"], ondelete="CASCADE",
                                name="fk_resume_extraction_results_profile"),
        sa.ForeignKeyConstraint(["resume_id"], ["uploaded_resumes.id"], ondelete="CASCADE",
                                name="fk_resume_extraction_results_resume"),
        sa.PrimaryKeyConstraint("id", name="pk_resume_extraction_results"),
    )
    op.create_index(
        "ix_resume_extraction_results_owner_resume",
        "resume_extraction_results", ["profile_id", "resume_id"], unique=False,
    )


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM resume_extraction_results LIMIT 1"
    )).first():
        raise RuntimeError(
            "Refusing to discard durable resume extraction results; preserve local review data"
        )
    op.drop_index("ix_resume_extraction_results_owner_resume", table_name="resume_extraction_results")
    op.drop_table("resume_extraction_results")
