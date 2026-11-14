"""Persist user-reported submitted resume bindings and observed outcomes."""

from alembic import op
import sqlalchemy as sa


revision = "20261106ro01"
down_revision = "20261104rv01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resume_outcome_bindings",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("profile_id", sa.String(128), sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("application_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("document_sha256", sa.JSON(), nullable=False),
        sa.Column("reported_at", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.UniqueConstraint("profile_id", "application_id", name="uq_resume_outcome_application"),
        sa.CheckConstraint("document_version > 0", name="ck_resume_outcome_document_version"),
    )
    op.create_table(
        "resume_outcome_events",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("binding_id", sa.String(128), sa.ForeignKey("resume_outcome_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.UniqueConstraint("binding_id", "request_id", name="uq_resume_outcome_event_request"),
        sa.CheckConstraint(
            "kind IN ('RESPONSE','SCREEN','INTERVIEW','OFFER','REJECTED','WITHDRAWN')",
            name="ck_resume_outcome_event_kind",
        ),
    )
    op.create_index("ix_resume_outcome_profile_time", "resume_outcome_bindings",
                    ["profile_id", "reported_at"])
    op.create_index("ix_resume_outcome_binding_time", "resume_outcome_events",
                    ["binding_id", "observed_at"])


def downgrade():
    connection = op.get_bind()
    if (connection.execute(sa.text("SELECT 1 FROM resume_outcome_events LIMIT 1")).first()
            or connection.execute(sa.text("SELECT 1 FROM resume_outcome_bindings LIMIT 1")).first()):
        raise RuntimeError("Refusing to discard resume outcome history; restore a verified backup for intentional rollback")
    op.drop_index("ix_resume_outcome_binding_time", table_name="resume_outcome_events")
    op.drop_index("ix_resume_outcome_profile_time", table_name="resume_outcome_bindings")
    op.drop_table("resume_outcome_events")
    op.drop_table("resume_outcome_bindings")
