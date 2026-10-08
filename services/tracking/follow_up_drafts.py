"""Read-only, history-grounded application follow-up draft candidates."""
from datetime import datetime, timedelta, timezone
import re

from services.tracking.applications import list_application_tracking


_ALLOWED_ATTRIBUTIONS = {"USER_REPORTED", "EXTERNAL_USER_REPORTED"}
_CHARACTER_LIMITS = {"subject": 120, "email_body": 500, "dm_body": 280}
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+|\b(?:[A-Z0-9-]+\.)+[A-Z]{2,}(?:/[^\s<>]*)?")
_HANDLE = re.compile(r"(?<![\w.+-])@[A-Z0-9_][A-Z0-9_.-]*", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d().\s-]{5,}\d(?!\w)")


def _aware_datetime(value, *, label):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{label} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _scrub_phone(match):
    value = match.group(0)
    return " [redacted] " if sum(character.isdigit() for character in value) >= 7 else value


def _scrub_dynamic(value):
    if not isinstance(value, str):
        value = ""
    # Remove complete email tokens before domains so their fragments cannot remain.
    value = _EMAIL.sub(" [redacted] ", value)
    value = _URL.sub(" [redacted] ", value)
    value = _HANDLE.sub(" [redacted] ", value)
    value = _PHONE.sub(_scrub_phone, value)
    return re.sub(r"\s+", " ", value).strip()


def _latest_status(history):
    statuses = [event for event in history
                if isinstance(event, dict) and event.get("type") == "status"]
    if not statuses:
        return None
    # list_application_tracking already returns sequence/time/id ordered history.
    return statuses[-1]


def _bounded(value, limit):
    return value[:limit]


def draft_follow_up_candidates(store, profile_id, quiet_days, *, now=None):
    """Return deterministic drafts for quiet, user-reported applied statuses.

    This function only reads persisted applications and their append-only tracking
    history. Drafts are returned to the caller and are never persisted or sent.
    """
    if isinstance(quiet_days, bool) or not isinstance(quiet_days, int) or not 1 <= quiet_days <= 90:
        raise ValueError("quiet_days must be an integer from 1 to 90")
    if now is None:
        clock = datetime.now(timezone.utc)
    else:
        if not isinstance(now, datetime):
            raise ValueError("now must be a timezone-aware datetime")
        clock = _aware_datetime(now, label="now")

    # Preserve the store's established missing-profile error contract.
    store.profile(profile_id)
    applications = store.applications(profile_id)
    tracking_by_application = list_application_tracking(
        store, profile_id, [row["id"] for row in applications])
    candidates = []
    threshold = timedelta(days=quiet_days)

    for row in applications:
        tracking = tracking_by_application.get(row["id"], {})
        latest = _latest_status(tracking.get("history", []))
        if (latest is None or latest.get("status") != "APPLIED"
                or latest.get("attribution") not in _ALLOWED_ATTRIBUTIONS):
            continue
        try:
            reported_at = _aware_datetime(latest.get("timestamp"), label="reported_at")
        except ValueError:
            continue
        elapsed = clock - reported_at
        if elapsed < threshold:
            continue

        title = _scrub_dynamic(row.get("title"))
        company = _scrub_dynamic(row.get("company"))
        date = reported_at.date().isoformat()
        subject = _bounded(f"Following up: {title} at {company}", _CHARACTER_LIMITS["subject"])
        email_body = _bounded(
            f"Hello,\n\nI applied for the {title} position at {company} on {date}. "
            "I’m checking whether there are any updates on my application. "
            "Thank you for your time.",
            _CHARACTER_LIMITS["email_body"],
        )
        dm_body = _bounded(
            f"Hi, I applied for {title} at {company} on {date}. "
            "I’m checking whether there are any updates. Thank you.",
            _CHARACTER_LIMITS["dm_body"],
        )
        candidates.append({
            "application_id": row["id"],
            "job_id": row["job_id"],
            "job_title": title,
            "company": company,
            "days_quiet": elapsed.days,
            "reported_at": latest["timestamp"],
            "attribution": latest["attribution"],
            "verified": False,
            "email_subject": subject,
            "email_body": email_body,
            "dm_body": dm_body,
            "character_limits": dict(_CHARACTER_LIMITS),
        })

    candidates.sort(key=lambda candidate: (candidate["reported_at"], candidate["application_id"]))
    return {"quiet_days": quiet_days, "candidates": candidates}
