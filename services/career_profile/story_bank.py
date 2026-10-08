"""Profile-scoped immutable user-authored career-story revisions."""
from __future__ import annotations

from collections import defaultdict
from math import isfinite
import re
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity, now


KINDS = frozenset({"STAR_STORY", "ACCOMPLISHMENT", "RESUME_BULLET"})
ALLOWED_USES = frozenset({"RESUME", "COVER_LETTER", "APPLICATION_ANSWER", "INTERVIEW_PREPARATION"})
MAX_FACTS = 50
MAX_TAGS = 20
_TAG = re.compile(r"^\S.{0,78}\S$|^\S$", re.DOTALL)


class StoryBankError(ValueError):
    """Sanitized story-bank error with a stable API-facing code and status."""

    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _fail(code: str, message: str, status_code: int = 422):
    raise StoryBankError(code, status_code, message)


def _require_profile(store, profile_id: str, connection=None, lock: bool = False) -> dict:
    try:
        if lock and connection is not None:
            owner = connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id,
            ).with_for_update()).scalar_one_or_none()
            if owner is None:
                _fail("PROFILE_NOT_FOUND", "Career profile was not found", 404)
        profile = store.profile(profile_id, connection=connection)
    except (NoResultFound, KeyError, TypeError, ValueError) as error:
        raise StoryBankError("PROFILE_NOT_FOUND", 404, "Career profile was not found") from error
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        _fail("PROFILE_NOT_FOUND", "Career profile was not found", 404)
    return profile


def _text(value, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        _fail("INVALID_STORY", f"{field} must be text")
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        _fail("INVALID_STORY", f"{field} must contain {minimum} to {maximum} characters")
    return value


def _normalize_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        _fail("INVALID_STORY", "Story content must be an object")
    allowed_fields = {"kind", "title", "text", "fact_ids", "tags", "allowed_uses"}
    if set(payload) != allowed_fields:
        _fail("INVALID_STORY", "Story content must provide exactly the supported fields")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        _fail("INVALID_STORY", "Story kind is not supported")
    title = _text(payload.get("title"), "title", 1, 200)
    text = _text(payload.get("text"), "text", 1, 8000)

    fact_ids = payload.get("fact_ids")
    if (not isinstance(fact_ids, list) or not 1 <= len(fact_ids) <= MAX_FACTS
            or any(not isinstance(item, str) or not item.strip() or len(item) > 128 for item in fact_ids)):
        _fail("INVALID_STORY", "Story must reference between 1 and 50 career facts")
    normalized_fact_ids = [item.strip() for item in fact_ids]
    if len(set(normalized_fact_ids)) != len(normalized_fact_ids):
        _fail("INVALID_STORY", "Story fact IDs must be unique")

    tags = payload.get("tags")
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        _fail("INVALID_STORY", "Story tags must be a list of at most 20 values")
    normalized_tags = []
    seen_tags = set()
    for tag in tags:
        if not isinstance(tag, str):
            _fail("INVALID_STORY", "Story tags must be text")
        tag = tag.strip()
        if not tag or len(tag) > 80 or not _TAG.fullmatch(tag):
            _fail("INVALID_STORY", "Each story tag must contain 1 to 80 non-whitespace characters")
        folded = tag.casefold()
        if folded in seen_tags:
            _fail("INVALID_STORY", "Story tags must be unique")
        seen_tags.add(folded)
        normalized_tags.append(tag)

    uses = payload.get("allowed_uses")
    if not isinstance(uses, list) or len(uses) > 4:
        _fail("INVALID_STORY", "Allowed uses must be a list of at most 4 values")
    if any(not isinstance(use, str) or use not in ALLOWED_USES for use in uses):
        _fail("INVALID_STORY", "Story contains an unsupported allowed use")
    if len(set(uses)) != len(uses):
        _fail("INVALID_STORY", "Allowed uses must be unique")

    return {"kind": kind, "title": title, "text": text,
            "fact_ids": normalized_fact_ids, "tags": normalized_tags,
            "allowed_uses": sorted(uses)}


def _fact_identity(fact: dict) -> str:
    fact_id = fact.get("fact_id", fact.get("id"))
    return identity("career-story-fact-v1", fact_id, fact.get("text"), fact.get("source"),
                    fact.get("verified"), fact.get("user_approved"), fact.get("confidence"))


def _fact_index(profile: dict) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = defaultdict(list)
    for row in profile.get("facts", []):
        if not isinstance(row, dict):
            continue
        fact_id = row.get("fact_id", row.get("id"))
        if isinstance(fact_id, str):
            index[fact_id].append(row)
    return index


def _fact_eligible(row: dict) -> bool:
    confidence = row.get("confidence")
    return (row.get("verified") is True and row.get("user_approved") is True
            and type(confidence) in (int, float) and isfinite(confidence) and 80 <= confidence <= 100
            and isinstance(row.get("text"), str) and bool(row["text"].strip())
            and isinstance(row.get("source"), str) and bool(row["source"].strip()))


def _validate_fact_links(profile: dict, fact_ids: list[str]) -> list[dict]:
    index = _fact_index(profile)
    snapshots = []
    missing = []
    ineligible = []
    for fact_id in fact_ids:
        matches = index.get(fact_id, [])
        if len(matches) != 1:
            missing.append(fact_id)
            continue
        fact = matches[0]
        if not _fact_eligible(fact):
            ineligible.append(fact_id)
            continue
        snapshots.append({"fact_id": fact_id, "fingerprint": _fact_identity(fact)})
    if missing:
        _fail("FACTS_UNAVAILABLE", "One or more selected facts are not in the current profile")
    if ineligible:
        _fail("FACT_NOT_APPROVED", "Every selected fact must be verified, approved, and supported")
    return snapshots


def _current_fact_fingerprints(profile: dict, fact_ids: list[str]) -> tuple[dict[str, str], list[str]]:
    index = _fact_index(profile)
    current = {}
    stale = []
    for fact_id in fact_ids:
        matches = index.get(fact_id, [])
        if len(matches) != 1 or not _fact_eligible(matches[0]):
            stale.append(fact_id)
            continue
        current[fact_id] = _fact_identity(matches[0])
    return current, stale


def _story_row(connection, profile_id: str, story_id: str):
    row = connection.execute(select(m.career_stories).where(
        m.career_stories.c.id == story_id,
        m.career_stories.c.profile_id == profile_id,
    )).mappings().one_or_none()
    if row is None:
        _fail("STORY_NOT_FOUND", "Career story was not found for this profile", 404)
    return dict(row)


def _latest_revision(connection, profile_id: str, story_key: str) -> int:
    revisions = connection.execute(select(m.career_stories.c.revision).where(
        m.career_stories.c.profile_id == profile_id,
        m.career_stories.c.story_key == story_key,
    )).scalars().all()
    if not revisions:
        _fail("STORY_NOT_FOUND", "Career story was not found for this profile", 404)
    return max(revisions)


def _lock_latest_revision(connection, profile_id: str, story_key: str) -> None:
    connection.execute(select(m.career_stories.c.id).where(
        m.career_stories.c.profile_id == profile_id,
        m.career_stories.c.story_key == story_key,
    ).order_by(m.career_stories.c.revision.desc()).limit(1).with_for_update()).first()


def _present(row: dict, profile: dict, latest_revision: int) -> dict:
    fact_ids = list(row["fact_ids"])
    current, stale = _current_fact_fingerprints(profile, fact_ids)
    stored = {entry.get("fact_id"): entry.get("fingerprint")
              for entry in row["fact_fingerprints"] if isinstance(entry, dict)}
    fingerprint_mismatch = [fact_id for fact_id in fact_ids
                            if fact_id in current and stored.get(fact_id) != current[fact_id]]
    stale_ids = sorted(set(stale + fingerprint_mismatch))
    is_latest = row["revision"] == latest_revision
    reusable = (is_latest and row["review_status"] == "APPROVED"
                and row["profile_version"] == profile["version"]
                and not stale_ids and bool(row["allowed_uses"]))
    return {
        "story_id": row["id"],
        "profile_id": row["profile_id"],
        "story_key": row["story_key"],
        "revision": row["revision"],
        "profile_version": row["profile_version"],
        "kind": row["kind"],
        "title": row["title"],
        "text": row["text"],
        "fact_ids": fact_ids,
        "fact_fingerprints": list(row["fact_fingerprints"]),
        "tags": list(row["tags"]),
        "allowed_uses": list(row["allowed_uses"]),
        "review_status": row["review_status"],
        "review_note": row["review_note"],
        "created_at": row["created_at"],
        "reviewed_at": row["reviewed_at"],
        "is_latest": is_latest,
        "reusable": reusable,
        "stale_fact_ids": stale_ids,
    }


def _present_all(connection, profile_id: str, profile: dict) -> list[dict]:
    rows = [dict(row) for row in connection.execute(select(m.career_stories).where(
        m.career_stories.c.profile_id == profile_id,
    )).mappings().all()]
    latest = {}
    for row in rows:
        latest[row["story_key"]] = max(latest.get(row["story_key"], 0), row["revision"])
    rows.sort(key=lambda row: (row["story_key"], -row["revision"]))
    return [_present(row, profile, latest[row["story_key"]]) for row in rows]


def _new_revision(connection, profile_id: str, story_key: str, revision: int,
                   profile: dict, content: dict) -> dict:
    row = {
        "id": str(uuid4()),
        "profile_id": profile_id,
        "story_key": story_key,
        "revision": revision,
        "profile_version": profile["version"],
        **content,
        "fact_fingerprints": _validate_fact_links(profile, content["fact_ids"]),
        "review_status": "PENDING",
        "review_note": None,
        "created_at": now(),
        "reviewed_at": None,
    }
    connection.execute(m.career_stories.insert().values(**row))
    return row


def create_story(store, profile_id: str, payload: dict) -> dict:
    """Create the first immutable, pending revision for a profile story."""
    content = _normalize_payload(payload)
    with store.engine.begin() as connection:
        profile = _require_profile(store, profile_id, connection, lock=True)
        story_key = str(uuid4())
        row = _new_revision(connection, profile_id, story_key, 1, profile, content)
        return _present(row, profile, 1)


def list_stories(store, profile_id: str) -> list[dict]:
    """List every immutable revision and mark only the current head reusable."""
    with store.engine.connect() as connection:
        profile = _require_profile(store, profile_id, connection)
        return _present_all(connection, profile_id, profile)


def review_story(store, profile_id: str, story_id: str, approved: bool,
                 reviewer_note: str | None, expected_revision: int) -> dict:
    """Make one explicit review decision on the current pending revision."""
    if type(approved) is not bool:
        _fail("INVALID_STORY", "Review decision must be a boolean")
    if type(expected_revision) is not int or expected_revision < 1:
        _fail("INVALID_STORY", "Expected revision must be a positive integer")
    if reviewer_note is not None:
        if not isinstance(reviewer_note, str) or len(reviewer_note) > 2000:
            _fail("INVALID_STORY", "Review note must be at most 2000 characters")
        reviewer_note = reviewer_note.strip() or None
    with store.engine.begin() as connection:
        profile = _require_profile(store, profile_id, connection, lock=True)
        row = _story_row(connection, profile_id, story_id)
        _lock_latest_revision(connection, profile_id, row["story_key"])
        latest = _latest_revision(connection, profile_id, row["story_key"])
        if row["revision"] != expected_revision or latest != expected_revision:
            _fail("REVISION_CONFLICT", "Story revision changed; reload before reviewing", 409)
        if row["review_status"] != "PENDING":
            _fail("REVIEW_ALREADY_FINAL", "This story revision has already been reviewed", 409)
        current_fingerprints = _validate_fact_links(profile, row["fact_ids"])
        stored_fingerprints = {
            entry.get("fact_id"): entry.get("fingerprint")
            for entry in row["fact_fingerprints"] if isinstance(entry, dict)
        }
        if any(stored_fingerprints.get(entry["fact_id"]) != entry["fingerprint"]
               for entry in current_fingerprints):
            _fail(
                "FACT_FINGERPRINT_MISMATCH",
                "One or more linked facts changed since this story revision was created",
            )
        reviewed_at = now()
        status = "APPROVED" if approved else "REJECTED"
        result = connection.execute(update(m.career_stories).where(
            m.career_stories.c.id == story_id,
            m.career_stories.c.profile_id == profile_id,
            m.career_stories.c.revision == expected_revision,
            m.career_stories.c.review_status == "PENDING",
        ).values(review_status=status, review_note=reviewer_note, reviewed_at=reviewed_at))
        if result.rowcount != 1:
            _fail("REVIEW_ALREADY_FINAL", "This story revision has already been reviewed", 409)
        row.update(review_status=status, review_note=reviewer_note, reviewed_at=reviewed_at)
        return _present(row, profile, latest)


def revise_story(store, profile_id: str, story_id: str, payload: dict,
                 expected_revision: int) -> dict:
    """Append a pending revision; prior story content and review state remain intact."""
    content = _normalize_payload(payload)
    if type(expected_revision) is not int or expected_revision < 1:
        _fail("INVALID_STORY", "Expected revision must be a positive integer")
    with store.engine.begin() as connection:
        profile = _require_profile(store, profile_id, connection, lock=True)
        prior = _story_row(connection, profile_id, story_id)
        _lock_latest_revision(connection, profile_id, prior["story_key"])
        latest = _latest_revision(connection, profile_id, prior["story_key"])
        if expected_revision != latest:
            _fail("REVISION_CONFLICT", "Story revision changed; reload before editing", 409)
        row = _new_revision(connection, profile_id, prior["story_key"], latest + 1, profile, content)
        return _present(row, profile, latest + 1)
