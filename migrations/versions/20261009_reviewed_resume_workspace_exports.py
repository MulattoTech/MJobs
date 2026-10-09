"""Persist the reviewed resume preview snapshot and idempotency key."""
from alembic import op
import sqlalchemy as sa


revision = "c4f08e16a2d9"
down_revision = "a7d19c0e4b26"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("resume_workspace_revisions", sa.Column("preview_fingerprint", sa.String(64)))
    op.add_column("resume_workspace_revisions", sa.Column("rendered_text", sa.Text()))
    op.add_column("resume_workspace_revisions", sa.Column("source_refs", sa.JSON()))
    op.add_column("resume_workspace_revisions", sa.Column("request_id", sa.String(128)))
    op.add_column("resume_workspace_revisions", sa.Column("payload_fingerprint", sa.String(64)))
    op.create_index(
        "uq_resume_workspace_profile_job_request",
        "resume_workspace_revisions",
        ["profile_id", "job_id", "request_id"],
        unique=True,
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT 1 FROM resume_workspace_revisions WHERE request_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError(
            "Refusing to discard reviewed resume workspace snapshots; "
            "restore a verified backup for intentional rollback"
        )
    op.drop_index("uq_resume_workspace_profile_job_request", table_name="resume_workspace_revisions")
    for name in ("payload_fingerprint", "request_id", "source_refs", "rendered_text", "preview_fingerprint"):
        op.drop_column("resume_workspace_revisions", name)
