"""Conservative time-window decisions for durable submission reservations."""

from datetime import datetime, timedelta


def company_submission_reserves_capacity(state, submitted_at, *, evaluated_at):
    """Expire only a confirmed submission with a proven, older aware timestamp.

    An unresolved submission may still have succeeded. Its reservation does not
    expire merely because time passed, including restored rows with no timestamp.
    Invalid or ambiguous confirmed timestamps likewise cannot prove expiry.
    """
    if (not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() is None):
        raise ValueError("Company submission window requires an aware clock")
    if state not in {"SUBMITTING", "CONFIRMED", "SUBMISSION_UNCONFIRMED"}:
        return False
    if state != "CONFIRMED":
        return True
    if not isinstance(submitted_at, str) or len(submitted_at) > 64:
        return True
    try:
        submitted = datetime.fromisoformat(submitted_at)
        if submitted.tzinfo is None or submitted.utcoffset() is None:
            return True
        # Match the existing daily window's inclusive lower boundary. Comparing
        # aware datetimes also handles equivalent instants with different offsets.
        return submitted >= evaluated_at - timedelta(days=1)
    except (ValueError, OverflowError):
        return True
