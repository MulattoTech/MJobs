"""Persist discovery retry deadlines without changing existing operation history."""
from alembic import op
import sqlalchemy as sa

revision = "20261011rt01"
down_revision = "20261011op01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("worker_operations", sa.Column("next_attempt_at", sa.String(64), nullable=True))
    op.create_index("ix_worker_operations_retry_due", "worker_operations", ["state", "next_attempt_at"])


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM worker_operations WHERE next_attempt_at IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Refusing to discard pending retry deadlines; preserve operation recovery state")
    op.drop_index("ix_worker_operations_retry_due", table_name="worker_operations")
    op.drop_column("worker_operations", "next_attempt_at")
