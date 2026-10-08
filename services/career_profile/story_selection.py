"""Read-only, job-specific selection of reusable career-story revisions."""
from __future__ import annotations

from services.career_profile.story_bank import StoryBankError, list_stories


ALLOWED_DOCUMENT_USES = frozenset({"RESUME", "COVER_LETTER"})
MAX_SELECTED_STORIES = 50


class StorySelectionError(ValueError):
    """Sanitized selector error with a stable API-facing code and status."""

    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _validate_request(job_id: str, allowed_use: str,
                      selected_story_ids: list[str] | None) -> list[str] | None:
    if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 128:
        raise StorySelectionError("INVALID_JOB", 422, "A valid job ID is required")
    if not isinstance(allowed_use, str) or allowed_use not in ALLOWED_DOCUMENT_USES:
        raise StorySelectionError(
            "INVALID_ALLOWED_USE", 422,
            "Story selection is limited to resume or cover-letter use",
        )
    if selected_story_ids is None:
        return None
    if (not isinstance(selected_story_ids, list)
            or len(selected_story_ids) > MAX_SELECTED_STORIES
            or any(not isinstance(item, str) or not item.strip() or len(item) > 128
                   for item in selected_story_ids)):
        raise StorySelectionError("INVALID_STORY_SELECTION", 422,
                                  "Selected story IDs must be a list of valid IDs")
    if len(set(selected_story_ids)) != len(selected_story_ids):
        raise StorySelectionError("INVALID_STORY_SELECTION", 422,
                                  "Selected story IDs must be unique")
    return list(selected_story_ids)


def _document_story(story: dict) -> dict:
    fingerprints = [dict(item) for item in story["fact_fingerprints"]]
    return {
        "story_id": story["story_id"],
        "story_key": story["story_key"],
        "revision": story["revision"],
        "profile_version": story["profile_version"],
        "kind": story["kind"],
        "title": story["title"],
        "text": story["text"],
        "fact_ids": list(story["fact_ids"]),
        "fact_fingerprints": fingerprints,
        "provenance": fingerprints,
        "tags": list(story["tags"]),
        "allowed_uses": list(story["allowed_uses"]),
        "review_status": story["review_status"],
        "reusable": True,
    }


def select_stories_for_job(store, profile_id: str, job_id: str, allowed_use: str,
                           selected_story_ids: list[str] | None = None) -> dict:
    """Offer eligible stories or validate an explicit ordered selection.

    A story is selectable only when the story-bank service reports its latest
    revision as approved and reusable against the current profile version and
    exact approved fact fingerprints. This function is read-only: it returns
    story and fact provenance without modifying CareerFacts or story revisions.
    When ``selected_story_ids`` is supplied, its order is preserved and every
    requested story must be eligible; invalid selections are never silently
    dropped.
    """
    selected_story_ids = _validate_request(job_id, allowed_use, selected_story_ids)
    try:
        revisions = list_stories(store, profile_id)
    except StoryBankError as error:
        raise StorySelectionError(error.code, error.status_code, str(error)) from error

    eligible = {
        story["story_id"]: story
        for story in revisions
        if (story.get("is_latest") is True
            and story.get("review_status") == "APPROVED"
            and story.get("reusable") is True
            and allowed_use in story.get("allowed_uses", []))
    }

    if selected_story_ids is None:
        selected = [eligible[story_id] for story_id in sorted(eligible)]
    else:
        missing = [story_id for story_id in selected_story_ids if story_id not in eligible]
        if missing:
            raise StorySelectionError(
                "STORY_NOT_SELECTABLE", 422,
                "One or more selected stories are unavailable for this document use",
            )
        selected = [eligible[story_id] for story_id in selected_story_ids]

    stories = [_document_story(story) for story in selected]
    return {
        "profile_id": profile_id,
        "job_id": job_id,
        "allowed_use": allowed_use,
        "story_ids": [story["story_id"] for story in stories],
        "stories": stories,
    }
