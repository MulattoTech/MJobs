"""Persist structured source-candidate evaluation on immutable review events."""
from alembic import op
import sqlalchemy as sa


revision = "9b6d40aaf163"
down_revision = "d5a7c81e240b"
branch_labels = None
depends_on = None

_LEGACY_NOTE = "Not evaluated before structured review support."


def upgrade():
    op.add_column("source_candidate_reviews", sa.Column(
        "api_feed_evidence_status", sa.String(length=32), nullable=True))
    op.add_column("source_candidate_reviews", sa.Column(
        "api_feed_evidence_reference", sa.String(length=2000), nullable=True))
    op.add_column("source_candidate_reviews", sa.Column(
        "api_feed_evidence_note", sa.String(length=1000), nullable=True))
    op.add_column("source_candidate_reviews", sa.Column(
        "access_terms_note", sa.String(length=1000), nullable=True))
    op.add_column("source_candidate_reviews", sa.Column(
        "capabilities_limits_note", sa.String(length=1000), nullable=True))
    op.add_column("source_candidate_reviews", sa.Column(
        "expected_value_maintenance_note", sa.String(length=1000), nullable=True))
    op.execute(sa.text(
        "UPDATE source_candidate_reviews SET api_feed_evidence_status = 'UNKNOWN', "
        "api_feed_evidence_note = :legacy, access_terms_note = :legacy, "
        "capabilities_limits_note = :legacy, expected_value_maintenance_note = :legacy"
    ).bindparams(legacy=_LEGACY_NOTE))
    with op.batch_alter_table("source_candidate_reviews") as batch:
        batch.alter_column("api_feed_evidence_status", existing_type=sa.String(length=32),
                           nullable=False)
        batch.alter_column("api_feed_evidence_note", existing_type=sa.String(length=1000),
                           nullable=False)
        batch.alter_column("access_terms_note", existing_type=sa.String(length=1000),
                           nullable=False)
        batch.alter_column("capabilities_limits_note", existing_type=sa.String(length=1000),
                           nullable=False)
        batch.alter_column("expected_value_maintenance_note", existing_type=sa.String(length=1000),
                           nullable=False)
        batch.create_check_constraint(
            "ck_source_candidate_review_evidence_status",
            "api_feed_evidence_status IN ('PUBLIC_DOCUMENTED', 'UNKNOWN', 'BLOCKED')",
        )


def downgrade():
    connection = op.get_bind()
    has_evaluated_review = connection.execute(sa.text(
        "SELECT 1 FROM source_candidate_reviews WHERE "
        "api_feed_evidence_status != 'UNKNOWN' OR api_feed_evidence_reference IS NOT NULL OR "
        "api_feed_evidence_note != :legacy OR access_terms_note != :legacy OR "
        "capabilities_limits_note != :legacy OR expected_value_maintenance_note != :legacy "
        "LIMIT 1"
    ).bindparams(legacy=_LEGACY_NOTE)).first()
    if has_evaluated_review:
        raise RuntimeError("Refusing to discard structured source-candidate review evidence")
    with op.batch_alter_table("source_candidate_reviews") as batch:
        batch.drop_constraint("ck_source_candidate_review_evidence_status", type_="check")
        batch.drop_column("expected_value_maintenance_note")
        batch.drop_column("capabilities_limits_note")
        batch.drop_column("access_terms_note")
        batch.drop_column("api_feed_evidence_note")
        batch.drop_column("api_feed_evidence_reference")
        batch.drop_column("api_feed_evidence_status")
