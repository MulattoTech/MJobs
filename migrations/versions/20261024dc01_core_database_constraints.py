"""Enforce match-score bounds and canonical application states.

Revision ID: 20261024dc01
Revises: 20261018cm01
Create Date: 2026-10-24
"""

import math

from alembic import op
import sqlalchemy as sa


revision = "20261024dc01"
down_revision = "20261018cm01"
branch_labels = None
depends_on = None


WRITER_APPLICATION_STATES = (
    "PREPARED",
    "FILLING",
    "AUTH_REQUIRED",
    "USER_INTERVENTION_REQUIRED",
    "READY_TO_SUBMIT",
    "DRY_RUN_COMPLETE",
    "REVIEW_REQUIRED",
    "SUBMITTING",
    "SUBMISSION_UNCONFIRMED",
    "CONFIRMED",
    "EXTERNALLY_APPLIED",
)
RESTORED_TERMINAL_APPLICATION_STATES = (
    "FAILED",
    "CANCELLED",
    "REJECTED",
    "BLOCKED",
    "DEAD_LETTERED",
    "DISCARDED",
    "UNKNOWN",
)
LEGACY_APPLICATION_STATES = ("DRAFT",)
DEPRECATED_AMBIGUOUS_APPLICATION_STATES = (
    "SUBMISSION_CONFIRMED",
    "APPLICATION_SUBMITTED",
)
RESTORE_NORMALIZED_APPLICATION_STATES = (
    "SUBMISSION_UNCONFIRMED",
    "REVIEW_REQUIRED",
)
APPLICATION_STATES = (
    *LEGACY_APPLICATION_STATES,
    *WRITER_APPLICATION_STATES,
    *RESTORED_TERMINAL_APPLICATION_STATES,
)
APPLICATION_STATE_CHECK = (
    "state IN (" + ",".join(f"'{state}'" for state in APPLICATION_STATES) + ")"
)


def _invalid_existing_row_counts(connection):
    invalid_scores = 0
    for score in connection.execute(sa.text("SELECT score FROM job_matches")).scalars():
        try:
            numeric = float(score)
        except (TypeError, ValueError, OverflowError):
            invalid_scores += 1
            continue
        if not math.isfinite(numeric) or not 0 <= numeric <= 100:
            invalid_scores += 1

    invalid_states = connection.execute(
        sa.text("SELECT COUNT(*) FROM applications WHERE state NOT IN :allowed_states")
        .bindparams(sa.bindparam("allowed_states", expanding=True)),
        {"allowed_states": APPLICATION_STATES},
    ).scalar_one()
    return invalid_scores, invalid_states


def upgrade():
    connection = op.get_bind()
    invalid_scores, invalid_states = _invalid_existing_row_counts(connection)
    if invalid_scores or invalid_states:
        raise RuntimeError(
            "Refusing to add core database constraints: "
            f"{invalid_scores} invalid job match score row(s); "
            f"{invalid_states} invalid application state row(s)"
        )

    with op.batch_alter_table("job_matches") as batch:
        batch.create_check_constraint(
            "ck_job_matches_score_range",
            "score >= 0 AND score <= 100",
        )
    with op.batch_alter_table("applications") as batch:
        batch.create_check_constraint(
            "ck_applications_state_valid",
            APPLICATION_STATE_CHECK,
        )


def downgrade():
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("ck_applications_state_valid", type_="check")
    with op.batch_alter_table("job_matches") as batch:
        batch.drop_constraint("ck_job_matches_score_range", type_="check")
