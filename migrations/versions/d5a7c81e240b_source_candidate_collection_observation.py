"""Allow explicitly typed supported-collection candidate observations."""
from alembic import op
import sqlalchemy as sa


revision = "d5a7c81e240b"
down_revision = "bc517d01a320"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("source_candidate_observations") as batch:
        batch.drop_constraint("ck_source_candidate_capture_method", type_="check")
        batch.create_check_constraint(
            "ck_source_candidate_capture_method",
            "capture_method IN ('MANUAL_CAPTURE', 'EXTERNAL_APPLICATION', 'SUPPORTED_COLLECTION')",
        )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
            "SELECT 1 FROM source_candidate_observations "
            "WHERE capture_method = 'SUPPORTED_COLLECTION' LIMIT 1")).first():
        raise RuntimeError("Refusing to discard supported-collection provenance")
    with op.batch_alter_table("source_candidate_observations") as batch:
        batch.drop_constraint("ck_source_candidate_capture_method", type_="check")
        batch.create_check_constraint(
            "ck_source_candidate_capture_method",
            "capture_method IN ('MANUAL_CAPTURE', 'EXTERNAL_APPLICATION')",
        )
