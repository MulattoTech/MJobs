"""Add immutable profile/job-scoped resume workspace revisions."""
from alembic import op
import sqlalchemy as sa


revision = "a7d19c0e4b26"
down_revision = "d0c97315c313"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resume_workspace_revisions",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column(
            "profile_id", sa.String(length=128),
            sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "job_id", sa.String(length=128),
            sa.ForeignKey("canonical_jobs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.String(length=64), nullable=False),
        sa.Column("job_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=100), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "profile_id", "job_id", "version",
            name="uq_resume_workspace_profile_job_version",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_resume_workspace_version_positive",
        ),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM resume_workspace_revisions LIMIT 1")
    ).first():
        raise RuntimeError(
            "Refusing to discard resume workspace revision history; "
            "restore a verified backup for intentional rollback"
        )
    op.drop_table("resume_workspace_revisions")
