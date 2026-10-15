"""Store imported discovery history as inert profile-owned snapshots."""
from alembic import op
import sqlalchemy as sa


revision = "20261014rh01"
down_revision = "20261013ai01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile_recovery_history_snapshots",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("profile_id", sa.String(length=128),
                  sa.ForeignKey("career_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("source_identity", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_recovery_history_schema_version"),
        sa.UniqueConstraint("profile_id", "kind", "source_identity",
                            name="uq_recovery_history_profile_kind_source"),
    )
    op.create_index("ix_recovery_history_profile_kind",
                    "profile_recovery_history_snapshots", ["profile_id", "kind"])


def downgrade():
    # Snapshots are the only recovered copy of historical content. Refuse to
    # silently discard them during a downgrade.
    bind = op.get_bind()
    if bind.execute(sa.text(
            "SELECT 1 FROM profile_recovery_history_snapshots LIMIT 1")).first():
        raise RuntimeError("Refusing to discard recovered profile history")
    op.drop_index("ix_recovery_history_profile_kind",
                  table_name="profile_recovery_history_snapshots")
    op.drop_table("profile_recovery_history_snapshots")
