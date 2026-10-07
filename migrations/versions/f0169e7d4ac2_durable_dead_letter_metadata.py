"""Persist dead-letter operation metadata."""
from alembic import op
import sqlalchemy as sa


revision = "f0169e7d4ac2"
down_revision = "e5019c4a7b20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("worker_operations", sa.Column("last_attempt_at", sa.String(length=64)))
    op.add_column("worker_operations", sa.Column("dead_lettered_at", sa.String(length=64)))
    op.add_column("worker_operations", sa.Column("discarded_at", sa.String(length=64)))


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
            "SELECT 1 FROM worker_operations WHERE state IN ('DEAD_LETTERED', 'DISCARDED') "
            "OR dead_lettered_at IS NOT NULL OR discarded_at IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError(
            "Refusing to discard dead-letter state; resolve or back up queued operations before rollback"
        )
    op.drop_column("worker_operations", "discarded_at")
    op.drop_column("worker_operations", "dead_lettered_at")
    op.drop_column("worker_operations", "last_attempt_at")
