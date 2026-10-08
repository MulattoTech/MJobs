"""Deterministic, persisted interview-preparation packs grounded in reviewed stories."""
from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.career_profile.story_bank import _present_all, _require_profile
from services.normalization.jobs import canonical_job_view


MAX_SELECTED_STORIES = 50
_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class InterviewPreparationError(ValueError):
    """Sanitized interview-preparation error with a stable API-facing code."""

    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _fail(code: str, message: str, status_code: int = 422):
    raise InterviewPreparationError(code, status_code, message)


def _validate_request(profile_id: str, job_id: str,
                      selected_story_revision_ids: list[str]) -> list[str]:
    if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 128:
        _fail("INVALID_PROFILE", "A valid profile ID is required")
    if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 128:
        _fail("INVALID_JOB", "A valid job ID is required")
    if (not isinstance(selected_story_revision_ids, list)
            or len(selected_story_revision_ids) > MAX_SELECTED_STORIES
            or any(not isinstance(item, str) or not item.strip() or len(item) > 128
                   for item in selected_story_revision_ids)):
        _fail("INVALID_STORY_SELECTION", "Selected story revision IDs must be a list of valid IDs")
    if len(set(selected_story_revision_ids)) != len(selected_story_revision_ids):
        _fail("INVALID_STORY_SELECTION", "Selected story revision IDs must be unique")
    return list(selected_story_revision_ids)


def _job_for_profile(connection, job_id: str) -> dict:
    row = connection.execute(
        select(m.jobs, m.companies.c.name.label("company_name"))
        .join(m.companies, m.jobs.c.company_id == m.companies.c.id)
        .where(m.jobs.c.id == job_id)
    ).mappings().one_or_none()
    if row is None:
        _fail("JOB_NOT_FOUND", "Job was not found", 404)
    return canonical_job_view(dict(row))


def _job_projection(job: dict) -> dict:
    """Only content that can shape this pack contributes to its job fingerprint."""
    return {
        "job_id": job.get("id"),
        "title": job.get("title"),
        "company_id": job.get("company_id"),
        "company_name": job.get("company_name"),
        "description": job.get("description_text") or "",
        "responsibilities": list(job.get("responsibilities") or []),
        "required_qualifications": list(job.get("required_qualifications") or []),
        "preferred_qualifications": list(job.get("preferred_qualifications") or []),
        "skills": list(job.get("skills") or []),
    }


def _job_topics(job: dict) -> list[str]:
    raw = []
    for field in ("responsibilities", "required_qualifications", "preferred_qualifications", "skills"):
        value = job.get(field)
        if isinstance(value, list):
            raw.extend(value)
        elif isinstance(value, str):
            raw.append(value)
    # Descriptions can be the only available job evidence. Keep bounded lines and
    # do not turn arbitrary long provider text into generated claims.
    if not raw:
        raw = [line.strip(" \t-*:")
               for line in (job.get("description_text") or "").splitlines()
               if line.strip() and len(line.strip()) <= 240]
    if not raw and job.get("title"):
        raw = [job["title"]]
    topics = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        topic = " ".join(item.split()).strip(" \t-*:")
        folded = topic.casefold()
        if topic and folded not in seen:
            seen.add(folded)
            topics.append(topic)
        if len(topics) == 20:
            break
    return topics


def _story_sources(story: dict) -> list[str]:
    refs = [f"career_story_revision:{story['story_id']}"]
    refs.extend(f"career_fact:{fact_id}" for fact_id in story["fact_ids"])
    return refs


def _linked_fact_texts(profile: dict, story: dict) -> list[str]:
    selected = set(story["fact_ids"])
    return [fact.get("text", "") for fact in profile.get("facts", [])
            if isinstance(fact, dict)
            and fact.get("fact_id", fact.get("id")) in selected
            and isinstance(fact.get("text"), str)]


def _has_topic_evidence(topic: str, profile: dict, stories: list[dict]) -> bool:
    topic_tokens = {token.casefold() for token in _TOKEN.findall(topic)}
    for story in stories:
        source_text = " ".join([story["title"], story["text"], *_linked_fact_texts(profile, story)]).casefold()
        if topic.casefold() in source_text:
            return True
        story_tokens = {token.casefold() for token in _TOKEN.findall(source_text)}
        if topic_tokens and topic_tokens.issubset(story_tokens):
            return True
    return False


def _build_content(profile_id: str, profile: dict, job: dict, job_fingerprint: str,
                   stories: list[dict], selected_ids: list[str]) -> dict:
    job_ref = f"canonical_job:{job['id']}:{job_fingerprint}"
    topics = _job_topics(job)
    questions = []
    for topic in topics:
        prompt = f"How have you approached {topic} in your work?"
        questions.append({
            "id": identity("interview-question-v1", job_fingerprint, prompt)[:24],
            "prompt": prompt,
            "source_refs": [job_ref],
        })
    for story in stories:
        prompt = f"Tell me about a time you demonstrated: {story['title']}"
        questions.append({
            "id": identity("interview-question-v1", story["story_id"], prompt)[:24],
            "prompt": prompt,
            "source_refs": _story_sources(story),
        })

    talking_points = [{
        "text": story["text"],
        "source_refs": _story_sources(story),
    } for story in stories]

    star_prompts = [{
        "question": f"Describe the experience titled: {story['title']}",
        "situation_prompt": "What was the situation?",
        "task_prompt": "What responsibility or goal did you have?",
        "action_prompt": "What actions did you personally take?",
        "result_prompt": "What result can you support with evidence?",
        "source_refs": _story_sources(story),
    } for story in stories]

    missing_evidence = [
        {"topic": topic,
         "reason": "No selected current approved interview story links evidence for this job topic."}
        for topic in topics if not _has_topic_evidence(topic, profile, stories)
    ]
    if not stories:
        missing_evidence.append({
            "topic": "Interview examples",
            "reason": "No current approved INTERVIEW_PREPARATION story revisions were selected.",
        })

    export_lines = [
        f"Interview preparation: {job.get('title') or 'Untitled role'} at {job.get('company_name') or 'Unknown company'}",
        f"Job ID: {job['id']}",
        "",
        "Likely questions",
    ]
    export_lines.extend(f"- {item['prompt']}" for item in questions)
    export_lines.extend(["", "Grounded talking points"])
    if talking_points:
        export_lines.extend(f"- {item['text']}" for item in talking_points)
    else:
        export_lines.append("- No selected current approved story revisions.")
    export_lines.extend(["", "STAR prompts"])
    for item in star_prompts:
        export_lines.extend([
            f"- {item['question']}",
            f"  Situation: {item['situation_prompt']}",
            f"  Task: {item['task_prompt']}",
            f"  Action: {item['action_prompt']}",
            f"  Result: {item['result_prompt']}",
        ])
    export_lines.extend(["", "Missing evidence"])
    export_lines.extend(f"- {item['topic']}: {item['reason']}" for item in missing_evidence)
    export_text = "\n".join(export_lines).rstrip() + "\n"
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "profile_version": profile["version"],
        "job_id": job["id"],
        "job_fingerprint": job_fingerprint,
        "selected_story_revision_ids": selected_ids,
        "questions": questions,
        "talking_points": talking_points,
        "star_prompts": star_prompts,
        "missing_evidence": missing_evidence,
        "export_text": export_text,
    }


def _eligible_stories(connection, store, profile_id: str, selected_ids: list[str]) -> tuple[dict, list[dict]]:
    profile = _require_profile(store, profile_id, connection, lock=True)
    all_stories = _present_all(connection, profile_id, profile)
    eligible = {
        story["story_id"]: story for story in all_stories
        if story.get("is_latest") is True
        and story.get("review_status") == "APPROVED"
        and story.get("reusable") is True
        and "INTERVIEW_PREPARATION" in story.get("allowed_uses", [])
    }
    unavailable = [story_id for story_id in selected_ids if story_id not in eligible]
    if unavailable:
        _fail("STORY_NOT_SELECTABLE", "One or more selected story revisions are unavailable for interview preparation")
    return profile, [eligible[story_id] for story_id in selected_ids]


def _present_pack(row: dict) -> dict:
    return {
        "schema_version": 1,
        "pack_id": row["id"],
        "pack_version": row["pack_version"],
        "profile_id": row["profile_id"],
        "profile_version": row["profile_version"],
        "job_id": row["job_id"],
        "job_fingerprint": row["job_fingerprint"],
        "created_at": row["created_at"],
        "selected_story_revision_ids": list(row["selected_story_revision_ids"]),
        "questions": list(row["questions"]),
        "talking_points": list(row["talking_points"]),
        "star_prompts": list(row["star_prompts"]),
        "missing_evidence": list(row["missing_evidence"]),
        "export_text": row["export_text"],
    }


def create_interview_prep_pack(store, profile_id: str, job_id: str,
                               selected_story_revision_ids: list[str]) -> dict:
    """Create or return the immutable current pack for this exact input tuple."""
    selected_ids = _validate_request(profile_id, job_id, selected_story_revision_ids)
    with store.engine.begin() as connection:
        profile, stories = _eligible_stories(connection, store, profile_id, selected_ids)
        job = _job_for_profile(connection, job_id)
        job_fingerprint = identity("interview-job-v1", _job_projection(job))
        existing = [dict(row) for row in connection.execute(
            select(m.interview_prep_packs).where(
                m.interview_prep_packs.c.profile_id == profile_id,
                m.interview_prep_packs.c.job_id == job_id,
            ).order_by(m.interview_prep_packs.c.pack_version.desc())
        ).mappings().all()]
        if existing:
            latest = existing[0]
            if (latest["profile_version"] == profile["version"]
                    and latest["job_fingerprint"] == job_fingerprint
                    and list(latest["selected_story_revision_ids"]) == selected_ids):
                return _present_pack(latest)
        next_version = max((row["pack_version"] for row in existing), default=0) + 1
        content = _build_content(profile_id, profile, job, job_fingerprint, stories, selected_ids)
        row = {
            "id": str(uuid4()),
            "profile_id": profile_id,
            "job_id": job_id,
            "pack_version": next_version,
            "profile_version": profile["version"],
            "job_fingerprint": job_fingerprint,
            "selected_story_revision_ids": selected_ids,
            "questions": content["questions"],
            "talking_points": content["talking_points"],
            "star_prompts": content["star_prompts"],
            "missing_evidence": content["missing_evidence"],
            "export_text": content["export_text"],
            "created_at": now(),
        }
        connection.execute(m.interview_prep_packs.insert().values(**row))
        return _present_pack(row)


def list_interview_prep_packs(store, profile_id: str, job_id: str) -> list[dict]:
    """List immutable packs for a profile/job in ascending version order."""
    if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 128:
        _fail("INVALID_PROFILE", "A valid profile ID is required")
    if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 128:
        _fail("INVALID_JOB", "A valid job ID is required")
    with store.engine.connect() as connection:
        if connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id)).scalar_one_or_none() is None:
            _fail("PROFILE_NOT_FOUND", "Career profile was not found", 404)
        _job_for_profile(connection, job_id)
        return [_present_pack(dict(row)) for row in connection.execute(
            select(m.interview_prep_packs).where(
                m.interview_prep_packs.c.profile_id == profile_id,
                m.interview_prep_packs.c.job_id == job_id,
            ).order_by(m.interview_prep_packs.c.pack_version)
        ).mappings().all()]


def get_interview_prep_pack(store, profile_id: str, job_id: str, pack_id: str) -> dict | None:
    """Read one profile- and job-scoped persisted pack, without mutating it."""
    if not all(isinstance(value, str) and value.strip() and len(value) <= 128
               for value in (profile_id, job_id, pack_id)):
        _fail("INVALID_REQUEST", "Profile, job, and pack IDs are required")
    with store.engine.connect() as connection:
        row = connection.execute(select(m.interview_prep_packs).where(
            m.interview_prep_packs.c.id == pack_id,
            m.interview_prep_packs.c.profile_id == profile_id,
            m.interview_prep_packs.c.job_id == job_id,
        )).mappings().one_or_none()
        return _present_pack(dict(row)) if row is not None else None
