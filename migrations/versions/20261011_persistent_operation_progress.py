"""Add durable progress and recovery metadata to worker operations."""
from alembic import op
import sqlalchemy as sa


revision = "20261011op01"
down_revision = "20261008ip01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("worker_operations") as batch:
        batch.add_column(sa.Column("current_step", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("progress", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("started_at", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(
            sa.Column("recovery_metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        )
        batch.create_check_constraint(
            "ck_worker_operations_progress_range", "progress >= 0 AND progress <= 100",
        )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT 1 FROM worker_operations WHERE current_step IS NOT NULL OR progress <> 0 "
        "OR started_at IS NOT NULL OR completed_at IS NOT NULL OR retry_count <> 0 "
        "OR CAST(recovery_metadata AS VARCHAR) <> '{}' LIMIT 1"
    )).first():
        raise RuntimeError(
            "Refusing to discard operation progress or recovery history; preserve the database before rollback"
        )
    with op.batch_alter_table("worker_operations") as batch:
        batch.drop_constraint("ck_worker_operations_progress_range", type_="check")
        batch.drop_column("recovery_metadata")
        batch.drop_column("retry_count")
        batch.drop_column("completed_at")
        batch.drop_column("started_at")
        batch.drop_column("progress")
        batch.drop_column("current_step")
