"""Read-only text rendering and keyword evidence for saved resume workspaces."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from services.documents.alignment_preview import build_alignment_preview
from services.documents.errors import PreparationError
from services.documents.resume_templates import select_resume_template
from services.documents.resume_workspace import list_resume_workspace_revisions
from services.documents.tailor import (
    eligible_rows,
    source_fact_fingerprint,
    target_fingerprint,
)
from services.documents.templates import HEADINGS, contact_header


_SECTION_FIELDS = {"section_id", "visible", "position", "fact_ids"}


def _fail(code: str, message: str):
    raise PreparationError(code, message)


def _validate_snapshot(profile, job, expected_profile_version, expected_job_fingerprint):
    if (not isinstance(profile, dict) or type(profile.get("version")) is not str or
            type(expected_profile_version) is not str or
            profile["version"] != expected_profile_version):
        _fail("RESUME_WORKSPACE_PROFILE_STALE",
              "The profile changed; reload the current approved facts before previewing.")
    if (not isinstance(job, dict) or type(job.get("id")) is not str or
            type(expected_job_fingerprint) is not str):
        _fail("RESUME_WORKSPACE_JOB_STALE",
              "The saved job snapshot is unavailable; reload it before previewing.")
    try:
        current_fingerprint = target_fingerprint(job)
    except (KeyError, TypeError, ValueError):
        _fail("RESUME_WORKSPACE_JOB_STALE",
              "The saved job snapshot is unsupported; reload it before previewing.")
    if current_fingerprint != expected_job_fingerprint:
        _fail("RESUME_WORKSPACE_JOB_STALE",
              "The job changed; reload the saved job before previewing.")
    return current_fingerprint


def _validate_sections(sections, template, facts_by_id):
    order = template["layout"]["section_order"]
    if type(sections) is not list or not sections or len(sections) > 50:
        _fail("RESUME_WORKSPACE_STALE_CONTEXT",
              "The saved workspace layout is unavailable or invalid.")
    normalized = []
    visible_sections = []
    visible_fact_ids = []
    seen_sections = set()
    for position, section in enumerate(sections):
        if (type(section) is not dict or set(section) != _SECTION_FIELDS or
                type(section.get("section_id")) is not str or
                section["section_id"] not in order or
                section["section_id"] not in HEADINGS or
                section["section_id"] in seen_sections or
                type(section.get("visible")) is not bool or
                type(section.get("position")) is not int or section["position"] != position):
            _fail("RESUME_WORKSPACE_STALE_CONTEXT",
                  "The saved workspace layout no longer matches a supported template.")
        fact_ids = section.get("fact_ids")
        if (type(fact_ids) is not list or not fact_ids or len(fact_ids) > 100 or
                any(type(fact_id) is not str or not fact_id.strip() or len(fact_id) > 128
                    for fact_id in fact_ids) or len(set(fact_ids)) != len(fact_ids) or
                not set(fact_ids).issubset(facts_by_id)):
            _fail("RESUME_WORKSPACE_STALE_CONTEXT",
                  "A saved fact reference is no longer current and approved.")
        item = {
            "section_id": section["section_id"],
            "visible": section["visible"],
            "position": position,
            "fact_ids": list(fact_ids),
        }
        normalized.append(item)
        seen_sections.add(item["section_id"])
        if item["visible"]:
            visible_sections.append(item)
            for fact_id in item["fact_ids"]:
                if fact_id not in visible_fact_ids:
                    visible_fact_ids.append(fact_id)
    if not visible_sections:
        _fail("RESUME_WORKSPACE_STALE_CONTEXT",
              "At least one saved resume section must be visible.")
    return normalized, visible_sections, visible_fact_ids


def build_resume_workspace_preview(store, profile_id: str, job_id: str, *,
                                   workspace_version: int,
                                   expected_profile_version: str,
                                   expected_job_fingerprint: str,
                                   expected_template_version: str) -> dict:
    """Render one persisted workspace revision from current approved profile facts.

    The preview is a text-only projection of existing facts and saved layout. Keyword
    alignment is returned as informational evidence and never changes resume text.
    """
    if (type(profile_id) is not str or not profile_id.strip() or
            type(job_id) is not str or not job_id.strip() or
            type(workspace_version) is not int or workspace_version < 1):
        _fail("RESUME_WORKSPACE_STALE_CONTEXT",
              "Choose an existing profile, job, and saved workspace version.")

    try:
        profile = store.profile(profile_id)
        job = store.job(job_id, profile_id)
    except Exception:
        _fail("RESUME_WORKSPACE_STALE_CONTEXT",
              "The requested profile/job workspace is unavailable.")
    current_job_fingerprint = _validate_snapshot(
        profile, job, expected_profile_version, expected_job_fingerprint)

    try:
        rows = list_resume_workspace_revisions(store, profile_id, job_id)
    except Exception as error:
        code = getattr(error, "code", None)
        if code in {"RESUME_WORKSPACE_INVALID", "RESUME_WORKSPACE_STALE_VERSION"}:
            _fail("RESUME_WORKSPACE_STALE_CONTEXT",
                  "The saved workspace is unavailable; reload it before previewing.")
        raise
    revision = next((row for row in rows if row.get("version") == workspace_version), None)
    if revision is None:
        _fail("RESUME_WORKSPACE_STALE_CONTEXT",
              "The requested workspace revision no longer exists.")
    if (revision.get("profile_id") != profile_id or revision.get("job_id") != job_id or
            revision.get("profile_version") != expected_profile_version or
            revision.get("job_fingerprint") != current_job_fingerprint):
        _fail("RESUME_WORKSPACE_STALE_CONTEXT",
              "The saved workspace belongs to an older profile or job snapshot.")

    try:
        template = select_resume_template(
            revision.get("template_id"), expected_template_version)
    except PreparationError:
        raise
    except (TypeError, ValueError):
        _fail("RESUME_TEMPLATE_VERSION_STALE",
              "Reload the template catalog and select its current version.")

    eligible = eligible_rows(profile)
    facts_by_id = {row.get("fact_id", row.get("id")): row for row in eligible}
    sections, visible_sections, visible_fact_ids = _validate_sections(
        revision.get("sections"), template, facts_by_id)

    rendered_sections = []
    source_facts = []
    for section in visible_sections:
        facts = [facts_by_id[fact_id] for fact_id in section["fact_ids"]]
        text = "\n".join(row["text"] for row in facts)
        rendered_sections.append({
            "section_id": section["section_id"],
            "heading": HEADINGS[section["section_id"]],
            "fact_ids": list(section["fact_ids"]),
            "text": text,
        })
        for row in facts:
            source_facts.append({
                "fact_id": row.get("fact_id", row.get("id")),
                "source": row["source"],
                "kind": row["kind"],
                "profile_version": expected_profile_version,
                "fact_fingerprint": source_fact_fingerprint(row),
            })

    preview_lines = contact_header(profile, revision["template_id"])
    for section in rendered_sections:
        preview_lines.append(section["heading"])
        if section["text"]:
            preview_lines.extend(section["text"].split("\n"))
    preview_text = "\n".join(preview_lines)

    try:
        alignment = build_alignment_preview(
            profile, job, expected_profile_version, current_job_fingerprint,
            selected_fact_ids=visible_fact_ids)
    except ValueError as error:
        _fail("RESUME_WORKSPACE_PREVIEW_INVALID",
              "Keyword alignment is unavailable for this saved snapshot.")
    rank = {fact_id: position for position, fact_id in enumerate(visible_fact_ids)}
    for group in alignment["matches"].values():
        for match in group:
            match["fact_ids"].sort(key=rank.__getitem__)
    alignment["selected_fact_ids"] = list(visible_fact_ids)

    # Keep the response bound to one current snapshot even if the profile or job
    # changes while the preview is being assembled.
    try:
        current_profile = store.profile(profile_id)
        current_job = store.job(job_id, profile_id)
    except Exception:
        _fail("RESUME_WORKSPACE_STALE_CONTEXT",
              "The requested profile/job workspace changed during preview.")
    _validate_snapshot(current_profile, current_job,
                       expected_profile_version, expected_job_fingerprint)

    return {
        "profile_id": profile_id,
        "profile_version": expected_profile_version,
        "job_id": job_id,
        "job_fingerprint": current_job_fingerprint,
        "workspace_version": workspace_version,
        "template": template,
        "sections": deepcopy(sections),
        "rendered_sections": rendered_sections,
        "preview_text": preview_text,
        "source_refs": {"facts": source_facts},
        "keyword_alignment": {
            "matches": alignment["matches"],
            "unmatched": alignment["unmatched"],
            "selected_fact_ids": list(visible_fact_ids),
            "analysis_only": True,
        },
        "read_only": True,
        "text_only": True,
        "ats_safe": True,
        "document_generated": False,
    }


def build_resume_workspace_draft_preview(store, profile_id: str, job_id: str, *,
                                         template_id: str, sections: list[dict],
                                         expected_profile_version: str,
                                         expected_job_fingerprint: str,
                                         expected_template_version: str) -> dict:
    """Render an unsaved reviewed layout without creating a revision or document."""
    if (type(profile_id) is not str or not profile_id.strip() or
            type(job_id) is not str or not job_id.strip()):
        _fail("RESUME_WORKSPACE_STALE_CONTEXT", "Choose a current profile and job.")
    try:
        profile = store.profile(profile_id)
        job = store.job(job_id, profile_id)
    except Exception:
        _fail("RESUME_WORKSPACE_STALE_CONTEXT", "The requested profile/job workspace is unavailable.")
    job_fingerprint = _validate_snapshot(
        profile, job, expected_profile_version, expected_job_fingerprint)
    try:
        template = select_resume_template(template_id, expected_template_version)
    except PreparationError:
        raise
    except (TypeError, ValueError):
        _fail("RESUME_TEMPLATE_VERSION_STALE", "Reload the template catalog before previewing.")

    eligible = eligible_rows(profile)
    facts_by_id = {row.get("fact_id", row.get("id")): row for row in eligible}
    normalized, visible_sections, visible_fact_ids = _validate_sections(
        sections, template, facts_by_id)
    rendered_sections = []
    source_facts = []
    for section in visible_sections:
        facts = [facts_by_id[fact_id] for fact_id in section["fact_ids"]]
        rendered_sections.append({
            "section_id": section["section_id"],
            "heading": HEADINGS[section["section_id"]],
            "fact_ids": list(section["fact_ids"]),
            "text": "\n".join(row["text"] for row in facts),
        })
        for row in facts:
            source_facts.append({
                "fact_id": row.get("fact_id", row.get("id")),
                "source": row["source"],
                "kind": row["kind"],
                "profile_version": expected_profile_version,
                "fact_fingerprint": source_fact_fingerprint(row),
            })

    rendered_lines = contact_header(profile, template_id)
    for section in rendered_sections:
        rendered_lines.append(section["heading"])
        if section["text"]:
            rendered_lines.extend(section["text"].split("\n"))
    rendered_text = "\n".join(rendered_lines)
    try:
        alignment = build_alignment_preview(
            profile, job, expected_profile_version, job_fingerprint,
            selected_fact_ids=visible_fact_ids)
    except ValueError:
        _fail("RESUME_WORKSPACE_PREVIEW_INVALID", "Keyword analysis is unavailable for this snapshot.")
    order = {fact_id: index for index, fact_id in enumerate(visible_fact_ids)}
    for group in alignment["matches"].values():
        for match in group:
            match["fact_ids"].sort(key=order.__getitem__)
    keyword_alignment = {
        "matches": alignment["matches"],
        "unmatched": alignment["unmatched"],
        "selected_fact_ids": list(visible_fact_ids),
        "analysis_only": True,
    }
    source_refs = {"facts": source_facts}
    preview_material = {
        "profile_id": profile_id,
        "profile_version": expected_profile_version,
        "job_id": job_id,
        "job_fingerprint": job_fingerprint,
        "template_id": template_id,
        "template_version": expected_template_version,
        "sections": normalized,
        "rendered_text": rendered_text,
        "source_refs": source_refs,
    }
    preview_fingerprint = hashlib.sha256(json.dumps(
        preview_material, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    try:
        current_profile = store.profile(profile_id)
        current_job = store.job(job_id, profile_id)
    except Exception:
        _fail("RESUME_WORKSPACE_STALE_CONTEXT", "The profile or job changed during preview.")
    _validate_snapshot(current_profile, current_job,
                       expected_profile_version, expected_job_fingerprint)
    return {
        **preview_material,
        "preview_fingerprint": preview_fingerprint,
        "sections": normalized,
        "rendered_sections": rendered_sections,
        "keyword_alignment": keyword_alignment,
        "read_only": True,
        "text_only": True,
        "ats_safe": True,
        "document_generated": False,
    }
