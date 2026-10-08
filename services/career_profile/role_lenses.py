"""Versioned, occupation-agnostic ranking lenses over approved profile evidence."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re

from sqlalchemy.exc import NoResultFound

from packages.persistence.store import identity
from services.career_profile.story_bank import StoryBankError, list_stories


SCHEMA_VERSION = 1
MAX_ROLE_TEXT_LENGTH = 256
MAX_PROFILE_ID_LENGTH = 128
MAX_EMPHASIZED_EVIDENCE = 3
DOCUMENT_STORY_USES = frozenset({"RESUME", "COVER_LETTER"})


class RoleLensError(ValueError):
    """Sanitized role-lens error with a stable code and HTTP-style status."""

    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class _LensDefinition:
    lens_id: str
    display_name: str
    description: str
    role_terms: tuple[str, ...]
    focus_terms: tuple[str, ...]
    fact_kind_order: tuple[str, ...]
    version: int = 1


_GENERAL_KINDS = (
    "accomplishment", "project", "responsibility", "technology_experience",
    "experience", "certification", "skill", "education", "summary",
)

_LENSES = (
    _LensDefinition(
        "support", "Support",
        "Prioritize approved evidence about user support, service delivery, and issue resolution.",
        (
            "help desk", "helpdesk", "service desk", "technical support",
            "user support", "customer support", "desktop support",
            "support technician", "support specialist", "support analyst", "support",
        ),
        (
            "support", "help desk", "helpdesk", "service desk", "ticket",
            "troubleshoot", "resolve", "incident", "user", "customer",
            "service", "desktop",
        ),
        ("responsibility", "accomplishment", "experience", "technology_experience",
         "project", "skill", "certification", "education", "summary"),
    ),
    _LensDefinition(
        "systems", "Systems",
        "Prioritize approved evidence about systems administration and infrastructure operations.",
        (
            "system administrator", "systems administrator", "sysadmin",
            "infrastructure", "server administrator", "network administrator",
            "systems engineer", "windows administrator",
        ),
        (
            "systems", "system", "server", "linux", "windows", "network",
            "infrastructure", "administer", "configuration", "active directory",
            "virtualization", "operations",
        ),
        ("technology_experience", "responsibility", "experience", "certification",
         "accomplishment", "project", "skill", "education", "summary"),
    ),
    _LensDefinition(
        "cloud", "Cloud",
        "Prioritize approved evidence about cloud platforms, automation, and reliability.",
        (
            "cloud", "devops", "site reliability", "platform engineer",
            "cloud engineer", "cloud administrator", "kubernetes", "sre",
        ),
        (
            "cloud", "aws", "azure", "gcp", "kubernetes", "terraform",
            "container", "devops", "platform", "deploy", "deployment",
            "automation", "reliability", "pipeline",
        ),
        ("project", "technology_experience", "accomplishment", "responsibility",
         "experience", "certification", "skill", "education", "summary"),
    ),
    _LensDefinition(
        "security", "Security",
        "Prioritize approved evidence about defensive security, identity, and risk reduction.",
        (
            "security", "cybersecurity", "cyber security", "infosec", "soc analyst",
            "security analyst", "security engineer", "vulnerability", "threat analyst",
            "incident response",
        ),
        (
            "security", "cybersecurity", "cyber", "vulnerability", "threat",
            "incident response", "identity", "access control", "iam", "audit",
            "firewall", "defense", "risk", "compliance",
        ),
        ("accomplishment", "responsibility", "technology_experience", "experience",
         "certification", "project", "skill", "education", "summary"),
    ),
    _LensDefinition(
        "general", "General",
        "Keep all currently approved evidence available with a stable general-purpose order.",
        (), (), _GENERAL_KINDS,
    ),
)
_LENS_BY_ID = {lens.lens_id: lens for lens in _LENSES}
_ROLE_TIEBREAK_ORDER = ("security", "cloud", "systems", "support")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TOKEN_BOUNDARY = r"(?<![a-z0-9]){}(?![a-z0-9])"


def list_role_lenses() -> list[dict]:
    """Return the stable public registry without exposing ranking internals."""
    return [
        {
            "id": lens.lens_id,
            "version": lens.version,
            "display_name": lens.display_name,
            "description": lens.description,
        }
        for lens in _LENSES
    ]


def _normalize_role_text(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_ROLE_TEXT_LENGTH:
        raise RoleLensError("ROLE_TEXT_INVALID", 422, "Role text is invalid")
    return " ".join(value.split()).casefold()


def _validate_lens_request(role_lens_id, expected_role_lens_version) -> None:
    if role_lens_id is not None and (
            not isinstance(role_lens_id, str) or not _ID_PATTERN.fullmatch(role_lens_id)):
        raise RoleLensError("ROLE_LENS_ID_INVALID", 422, "Role lens ID is invalid")
    if expected_role_lens_version is not None and (
            type(expected_role_lens_version) is not int or expected_role_lens_version < 1):
        raise RoleLensError(
            "ROLE_LENS_VERSION_INVALID", 422,
            "Expected role lens version must be a positive integer",
        )


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(_TOKEN_BOUNDARY.format(re.escape(phrase)), text, re.IGNORECASE) is not None


def _infer_lens(role_text: str) -> tuple[_LensDefinition, list[str]]:
    scored = []
    for lens in _LENSES:
        if lens.lens_id == "general":
            continue
        matched = [term for term in lens.role_terms if _contains_phrase(role_text, term)]
        if matched:
            tie_rank = _ROLE_TIEBREAK_ORDER.index(lens.lens_id)
            scored.append((-len(matched), tie_rank, lens, matched))
    if not scored:
        return _LENS_BY_ID["general"], []
    _score, _tie_rank, lens, matched = min(scored, key=lambda row: (row[0], row[1]))
    return lens, matched


def _fact_id(row: dict):
    return row.get("fact_id", row.get("id"))


def _fact_fingerprint(row: dict, fact_id: str) -> str:
    """Match the story bank's immutable fingerprint for one approved source fact."""
    return identity(
        "career-story-fact-v1", fact_id, row.get("text"), row.get("source"),
        row.get("verified"), row.get("user_approved"), row.get("confidence"),
    )


def _eligible_fact_index(profile: dict) -> dict[str, dict]:
    counts: dict[str, int] = {}
    rows: dict[str, dict] = {}
    facts = profile.get("facts")
    if not isinstance(facts, list):
        return {}
    for row in facts:
        if not isinstance(row, dict):
            continue
        fact_id = _fact_id(row)
        if not isinstance(fact_id, str) or not fact_id.strip() or len(fact_id) > 128:
            continue
        counts[fact_id] = counts.get(fact_id, 0) + 1
        confidence = row.get("confidence")
        if (
            row.get("verified") is True
            and row.get("user_approved") is True
            and type(confidence) in (int, float)
            and isfinite(confidence)
            and 80 <= confidence <= 100
            and isinstance(row.get("text"), str)
            and bool(row["text"].strip())
            and isinstance(row.get("source"), str)
            and bool(row["source"].strip())
        ):
            rows[fact_id] = dict(row, fact_id=fact_id)
    return {fact_id: row for fact_id, row in rows.items() if counts.get(fact_id) == 1}


def _rank_fact_rows(rows: dict[str, dict], lens: _LensDefinition):
    kind_rank = {kind: index for index, kind in enumerate(lens.fact_kind_order)}
    ranked = []
    matched_terms = {}
    for fact_id, row in rows.items():
        text = row["text"].casefold()
        matched = [term for term in lens.focus_terms if _contains_phrase(text, term)]
        confidence = row["confidence"]
        kind = row.get("kind")
        rank = kind_rank.get(kind, len(kind_rank))
        ranked.append(((-len(matched), rank, -float(confidence), fact_id), fact_id, matched))
    ranked.sort(key=lambda item: item[0])
    for _key, fact_id, matched in ranked:
        if matched:
            matched_terms[fact_id] = matched
    fact_ids = [fact_id for _key, fact_id, _matched in ranked]
    emphasized = (
        [fact_id for _key, fact_id, matched in ranked if matched][:MAX_EMPHASIZED_EVIDENCE]
        if lens.lens_id != "general" else []
    )
    return fact_ids, emphasized, matched_terms


def _story_source_ref(story: dict) -> dict:
    return {
        "story_id": story["story_id"],
        "story_key": story["story_key"],
        "revision": story["revision"],
        "profile_version": story["profile_version"],
        "fact_ids": list(story["fact_ids"]),
        "fact_fingerprints": [dict(item) for item in story["fact_fingerprints"]],
    }


def _current_usable_story(story: dict, profile_version: str,
                          eligible_facts: dict[str, dict], fact_fingerprints: dict[str, str]) -> bool:
    allowed_uses = story.get("allowed_uses")
    if (not isinstance(allowed_uses, list)
            or any(not isinstance(use, str) for use in allowed_uses)):
        return False
    if (
        story.get("is_latest") is not True
        or story.get("review_status") != "APPROVED"
        or story.get("reusable") is not True
        or story.get("profile_version") != profile_version
        or not (set(allowed_uses) & DOCUMENT_STORY_USES)
        or not isinstance(story.get("story_id"), str)
        or not story.get("story_id")
        or not isinstance(story.get("story_key"), str)
        or not story.get("story_key")
        or type(story.get("revision")) is not int
        or story["revision"] < 1
        or not isinstance(story.get("title"), str)
        or not story["title"].strip()
        or not isinstance(story.get("text"), str)
        or not story["text"].strip()
        or not isinstance(story.get("tags"), list)
        or any(not isinstance(tag, str) for tag in story["tags"])
    ):
        return False
    fact_ids = story.get("fact_ids")
    fingerprints = story.get("fact_fingerprints")
    if (not isinstance(fact_ids, list) or not fact_ids
            or any(not isinstance(fact_id, str) for fact_id in fact_ids)
            or len(set(fact_ids)) != len(fact_ids)
            or not isinstance(fingerprints, list)
            or any(not isinstance(item, dict) for item in fingerprints)):
        return False
    by_id = {
        item.get("fact_id"): item.get("fingerprint")
        for item in fingerprints
    }
    if len(by_id) != len(fingerprints):
        return False
    return all(
        isinstance(fact_id, str)
        and fact_id in eligible_facts
        and by_id.get(fact_id) == fact_fingerprints.get(fact_id)
        for fact_id in fact_ids
    )


def _rank_story_rows(stories: list[dict], lens: _LensDefinition):
    ranked = []
    matched_terms = {}
    for story in stories:
        story_id = story.get("story_id")
        searchable = " ".join([
            str(story.get("title") or ""), str(story.get("text") or ""),
            " ".join(str(tag) for tag in story.get("tags", []) if isinstance(tag, str)),
        ]).casefold()
        matched = [term for term in lens.focus_terms if _contains_phrase(searchable, term)]
        ranked.append(((-len(matched), story_id), story, matched))
    ranked.sort(key=lambda row: row[0])
    for _key, story, matched in ranked:
        if matched:
            matched_terms[story["story_id"]] = matched
    emphasized = (
        [story["story_id"] for _key, story, matched in ranked if matched][:MAX_EMPHASIZED_EVIDENCE]
        if lens.lens_id != "general" else []
    )
    return [story for _key, story, _matched in ranked], emphasized, matched_terms


def select_role_lens(store, profile_id: str, role_text: str | None, *,
                     role_lens_id: str | None = None,
                     expected_role_lens_version: int | None = None) -> dict:
    """Select a versioned presentation lens over current approved evidence.

    Role matching and evidence ordering are deterministic. The selector reads
    current CareerFacts and immutable story revisions itself and returns only
    source references to eligible facts and latest approved reusable stories.
    It never edits profile data or creates/rewrites source evidence.
    """
    if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > MAX_PROFILE_ID_LENGTH:
        raise RoleLensError("PROFILE_ID_INVALID", 422, "A valid profile ID is required")
    normalized_role = _normalize_role_text(role_text)
    _validate_lens_request(role_lens_id, expected_role_lens_version)
    if role_lens_id is not None:
        lens = _LENS_BY_ID.get(role_lens_id)
        if lens is None:
            raise RoleLensError("ROLE_LENS_NOT_FOUND", 404, "Role lens was not found")
        role_matches = [term for term in lens.role_terms if _contains_phrase(normalized_role, term)]
        selection_reason = "EXPLICIT"
    else:
        lens, role_matches = _infer_lens(normalized_role)
        selection_reason = "ROLE_MATCH" if role_matches else "GENERAL_FALLBACK"
    if (expected_role_lens_version is not None
            and expected_role_lens_version != lens.version):
        raise RoleLensError(
            "ROLE_LENS_VERSION_CONFLICT", 409,
            "The selected role lens changed; reload the supported lens list",
        )

    try:
        profile = store.profile(profile_id)
    except NoResultFound as error:
        raise RoleLensError("PROFILE_NOT_FOUND", 404, "Career profile was not found") from error
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        raise RoleLensError("PROFILE_NOT_FOUND", 404, "Career profile was not found")
    profile_version = profile.get("version")
    if not isinstance(profile_version, str) or not profile_version:
        raise RoleLensError("PROFILE_NOT_FOUND", 404, "Career profile was not found")
    try:
        revisions = list_stories(store, profile_id)
    except StoryBankError as error:
        raise RoleLensError(error.code, error.status_code, str(error)) from error

    eligible_facts = _eligible_fact_index(profile)
    fact_fingerprints = {
        fact_id: _fact_fingerprint(row, fact_id)
        for fact_id, row in eligible_facts.items()
    }
    fact_ids, emphasized_fact_ids, fact_matches = _rank_fact_rows(eligible_facts, lens)
    usable_stories = [
        story for story in revisions
        if isinstance(story, dict)
        and _current_usable_story(story, profile_version, eligible_facts, fact_fingerprints)
    ]
    stories, emphasized_story_ids, story_matches = _rank_story_rows(usable_stories, lens)

    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "profile_version": profile_version,
        "role_text": normalized_role,
        "lens": {
            "id": lens.lens_id,
            "version": lens.version,
            "display_name": lens.display_name,
            "description": lens.description,
        },
        "selection_reason": selection_reason,
        "matched_role_terms": role_matches,
        "fact_ids": fact_ids,
        "emphasized_fact_ids": emphasized_fact_ids,
        "story_ids": [story["story_id"] for story in stories],
        "emphasized_story_ids": emphasized_story_ids,
        "stories": [
            {
                "story_id": story["story_id"],
                "story_key": story["story_key"],
                "revision": story["revision"],
                "profile_version": story["profile_version"],
                "kind": story["kind"],
                "title": story["title"],
                "text": story["text"],
                "fact_ids": list(story["fact_ids"]),
                "fact_fingerprints": [dict(item) for item in story["fact_fingerprints"]],
                "tags": list(story["tags"]),
                "allowed_uses": list(story["allowed_uses"]),
                "review_status": story["review_status"],
                "reusable": True,
            }
            for story in stories
        ],
        "source_refs": {
            "facts": [
                {"fact_id": fact_id, "fingerprint": fact_fingerprints[fact_id]}
                for fact_id in fact_ids
            ],
            "stories": [_story_source_ref(story) for story in stories],
        },
        "matched_terms": {"facts": fact_matches, "stories": story_matches},
    }
