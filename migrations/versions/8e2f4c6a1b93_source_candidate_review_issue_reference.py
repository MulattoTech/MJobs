"""Preserve manually linked provider issue references in review history."""
from alembic import op
import sqlalchemy as sa


revision = "8e2f4c6a1b93"
down_revision = "e2a4c9d0b721"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "source_candidate_reviews",
        sa.Column("integration_issue", sa.Integer(), nullable=True),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT 1 FROM source_candidate_reviews "
        "WHERE integration_issue IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Refusing to discard source-candidate provider issue references")
    op.drop_column("source_candidate_reviews", "integration_issue")
