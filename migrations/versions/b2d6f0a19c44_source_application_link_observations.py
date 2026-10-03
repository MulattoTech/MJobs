"""Persist bounded source application-link observations."""
from alembic import op
import sqlalchemy as sa


revision = "b2d6f0a19c44"
down_revision = "a7c4e1b92d30"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_posting_value_candidates_quality", "posting_value_candidates", ["quality_observation_id"])
    op.create_table(
        "source_application_link_observations",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=128), sa.ForeignKey("posting_value_candidates.id"), nullable=False),
        sa.Column("observed_at", sa.String(length=64), nullable=False),
        sa.Column("requested_url", sa.String(length=2000), nullable=False),
        sa.Column("final_url", sa.String(length=2000)),
        sa.Column("result", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_sha256", sa.String(length=64)),
        sa.Column("identity_evidence", sa.JSON(), nullable=False),
        sa.UniqueConstraint("candidate_id"),
        sa.CheckConstraint("result IN ('ACCURATE', 'INACCURATE', 'UNKNOWN')", name="application_link_result_valid"),
    )
    op.create_index("ix_source_application_link_observed_at",
                    "source_application_link_observations", ["observed_at"])


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM source_application_link_observations LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard source application-link evidence; restore a verified backup for intentional rollback"
        )
    op.drop_index("ix_source_application_link_observed_at", table_name="source_application_link_observations")
    op.drop_table("source_application_link_observations")
    op.drop_index("ix_posting_value_candidates_quality", table_name="posting_value_candidates")
