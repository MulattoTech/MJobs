"""Persist append-only normalized posting value candidates."""
from alembic import op
import sqlalchemy as sa


revision = "a7c4e1b92d30"
down_revision = "f083a019c241"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('source_quality_observations', sa.Column('source_endpoint', sa.String(2000)))
    op.create_table(
        "posting_value_candidates",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("posting_id", sa.String(length=128), sa.ForeignKey("job_postings.id"), nullable=False),
        sa.Column("posting_snapshot_id", sa.String(length=128), sa.ForeignKey("posting_snapshots.id"), nullable=False),
        sa.Column("company_source_id", sa.String(length=128), sa.ForeignKey("company_job_sources.id"), nullable=False),
        sa.Column("quality_observation_id", sa.String(length=128), sa.ForeignKey("source_quality_observations.id"), nullable=False),
        sa.Column("observed_at", sa.String(length=64), nullable=False),
        sa.Column("normalized", sa.JSON(), nullable=False),
        sa.Column("application_url", sa.String(length=2000), nullable=False),
    )
    op.create_index(
        "ix_posting_value_candidates_posting_time",
        "posting_value_candidates", ["posting_id", "observed_at"],
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM posting_value_candidates LIMIT 1")).first():
        raise RuntimeError(
            "Refusing to discard posting value candidates; restore a verified backup for intentional rollback"
        )
    if connection.execute(sa.text('SELECT 1 FROM source_quality_observations WHERE source_endpoint IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Refusing to discard source quality evidence (observed origins); restore a verified backup for intentional rollback')
    op.drop_index("ix_posting_value_candidates_posting_time", table_name="posting_value_candidates")
    op.drop_table("posting_value_candidates")
    with op.batch_alter_table('source_quality_observations') as batch:
        batch.drop_column('source_endpoint')
