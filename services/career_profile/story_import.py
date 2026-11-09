"""Bounded, atomic JSON import of user-authored career stories."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from packages.persistence import models as m
from services.career_profile.story_bank import (
    StoryBankError,
    _fail,
    _latest_revision,
    _new_revision,
    _normalize_payload,
    _present,
    _require_profile,
    _validate_fact_links,
)

MAX_IMPORT_BYTES = 256 * 1024
MAX_IMPORT_STORIES = 20


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("INVALID_IMPORT", "Import JSON contains duplicate keys")
        value[key] = item
    return value


def _reject_constant(_value):
    _fail("INVALID_IMPORT", "Import JSON contains a non-finite number")


def _reject_unsafe_text(value):
    if isinstance(value, str):
        if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            _fail("INVALID_IMPORT", "Import JSON contains unsupported text")
    elif isinstance(value, list):
        for item in value:
            _reject_unsafe_text(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_unsafe_text(key)
            _reject_unsafe_text(item)


def parse_story_import(raw: bytes) -> tuple[list[dict], str]:
    """Parse one strict v1 document and hash its normalized content, not raw bytes."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_IMPORT_BYTES:
        _fail("INVALID_IMPORT", "Import document must be 1 to 262144 UTF-8 bytes")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_unsafe_text(document)
    except StoryBankError:
        raise
    except (ValueError, RecursionError) as error:
        raise StoryBankError("INVALID_IMPORT", 422, "Import document must be valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "stories"}:
        _fail("INVALID_IMPORT", "Import document must contain schema_version and stories")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        _fail("INVALID_IMPORT", "Import schema version is not supported")
    stories = document["stories"]
    if not isinstance(stories, list) or not 1 <= len(stories) <= MAX_IMPORT_STORIES:
        _fail("INVALID_IMPORT", "Import must contain 1 to 20 stories")
    normalized = [_normalize_payload(story) for story in stories]
    canonical = json.dumps(
        {"schema_version": 1, "stories": normalized},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return normalized, hashlib.sha256(canonical).hexdigest()


def import_stories(store, profile_id: str, raw: bytes) -> dict:
    """Insert pending revision-one rows as one transaction; replay never changes old rows."""
    stories, digest = parse_story_import(raw)
    results = []
    created_count = 0
    with store.engine.begin() as connection:
        # SQLite's deferred BEGIN would permit two importers to validate the same
        # empty key concurrently. Serialize before reading; PostgreSQL uses the
        # profile row lock below.
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        profile = _require_profile(store, profile_id, connection, lock=True)
        for index, content in enumerate(stories):
            current_fingerprints = _validate_fact_links(profile, content["fact_ids"])
            story_key = f"import-v1-{digest}-{index:02d}"
            original = connection.execute(select(m.career_stories).where(
                m.career_stories.c.profile_id == profile_id,
                m.career_stories.c.story_key == story_key,
                m.career_stories.c.revision == 1,
            )).mappings().one_or_none()
            created = original is None
            if created:
                row = _new_revision(connection, profile_id, story_key, 1, profile, content)
                created_count += 1
                latest = 1
            else:
                row = dict(original)
                stored = row["fact_fingerprints"]
                if stored != current_fingerprints:
                    _fail("FACT_FINGERPRINT_MISMATCH",
                          "One or more imported story facts changed since the original import")
                latest = _latest_revision(connection, profile_id, story_key)
            results.append({"created": created, **_present(row, profile, latest)})
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "canonical_sha256": digest,
        "created_count": created_count,
        "stories": results,
    }
