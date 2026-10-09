"""Read-only deterministic cover-letter previews bound to saved evidence."""
from __future__ import annotations

import hashlib
import json
import re

from services.documents import tailor
from services.documents.cover_letter import _approved_company_context, build_cover_letter
from services.documents.errors import PreparationError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_FACTS = 1000


def _fail(code: str, message: str):
    raise PreparationError(code, message)


def _job_snapshot(job: dict) -> dict:
    """Fingerprint every saved job value that can affect the preview."""
    if not isinstance(job, dict) or type(job.get("id")) is not str or not job["id"].strip():
        _fail("COVER_LETTER_PREVIEW_JOB_REQUIRED", "A saved target job is required")
    attributes = job.get("attributes") if isinstance(job.get("attributes"), dict) else {}
    try:
        target_fingerprint = tailor.target_fingerprint(job)
        snapshot = {
            "target_fingerprint": target_fingerprint,
            "id": job["id"],
            "company_id": job.get("company_id"),
            "company": job.get("company"),
            "title": job.get("title"),
            "skills": job.get("skills"),
            "description": job.get("description"),
            "responsibility_evidence": job.get("responsibility_evidence"),
            "attributes": {
                "skill_evidence": attributes.get("skill_evidence"),
                "required_skills": attributes.get("required_skills"),
                "preferred_skills": attributes.get("preferred_skills"),
                "company_identity": attributes.get("company_identity"),
                "approved_company_context": attributes.get("approved_company_context"),
            },
        }
        serialized = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        _fail("COVER_LETTER_PREVIEW_JOB_REQUIRED", "The saved target job snapshot is unsupported")
    return {
        "fingerprint": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "target_fingerprint": target_fingerprint,
        "snapshot": snapshot,
    }


def cover_letter_preview_fingerprint(job: dict) -> str:
    """Hash target, company, skill, responsibility, and context inputs together."""
    return _job_snapshot(job)["fingerprint"]


def _current_fact_rows(facts, profile) -> dict[str, dict]:
    current_facts = profile.get("facts")
    if (not isinstance(facts, list) or not isinstance(current_facts, list)
            or len(facts) > _MAX_FACTS or len(current_facts) > _MAX_FACTS):
        _fail("COVER_LETTER_PREVIEW_EVIDENCE_INVALID", "The current career-fact snapshot is unavailable")

    def index_rows(rows):
        indexed = {}
        for row in rows:
            if not isinstance(row, dict):
                _fail("COVER_LETTER_PREVIEW_EVIDENCE_INVALID", "A career-fact reference is invalid")
            fact_id = row.get("fact_id") or row.get("id")
            if (type(fact_id) is not str or not fact_id.strip() or len(fact_id) > 128
                    or fact_id in indexed):
                _fail("COVER_LETTER_PREVIEW_EVIDENCE_INVALID", "Career-fact references are missing or ambiguous")
            indexed[fact_id] = dict(row, fact_id=fact_id)
        return indexed

    supplied = index_rows(facts)
    current = index_rows(current_facts)
    if supplied.keys() != current.keys():
        _fail("COVER_LETTER_PREVIEW_EVIDENCE_INVALID", "Career facts do not match the current profile snapshot")
    for fact_id, row in supplied.items():
        if tailor.source_fact_fingerprint(row) != tailor.source_fact_fingerprint(current[fact_id]):
            _fail("COVER_LETTER_PREVIEW_EVIDENCE_INVALID", "A career fact changed outside the current profile snapshot")
    return supplied


def _validate_job_evidence(job: dict) -> None:
    if (type(job.get("title")) is not str or not job["title"].strip()
            or type(job.get("company")) is not str or not job["company"].strip()
            or type(job.get("description")) is not str):
        _fail("COVER_LETTER_JOB_CONTEXT_MISSING", "Saved job title, company, and posting description are required")
    skills = job.get("skills")
    if (not isinstance(skills, list) or not skills
            or any(type(skill) is not str or not skill.strip() or len(skill) > 100 for skill in skills)):
        _fail("COVER_LETTER_JOB_CONTEXT_MISSING", "Saved posting skill evidence is required")
    responsibilities = job.get("responsibility_evidence")
    if responsibilities is not None and (
        not isinstance(responsibilities, list)
        or any(type(item) is not str or not item.strip() for item in responsibilities)
    ):
        _fail("COVER_LETTER_JOB_CONTEXT_MISSING", "Saved responsibility evidence is unsupported")
    attributes = job.get("attributes") if isinstance(job.get("attributes"), dict) else {}
    for key in ("required_skills", "preferred_skills"):
        terms = attributes.get(key)
        if terms is not None and (
            not isinstance(terms, list)
            or any(type(term) is not str or not term.strip() for term in terms)
        ):
            _fail("COVER_LETTER_JOB_CONTEXT_MISSING", "Saved posting skill evidence is unsupported")


def build_cover_letter_preview(
    profile,
    job,
    facts,
    *,
    expected_profile_version,
    expected_job_fingerprint,
    style="professional",
    length="normal",
):
    """Build a deterministic draft and provenance without saving a document.

    `expected_job_fingerprint` must come from `cover_letter_preview_fingerprint`
    over the same current saved job snapshot. The profile version and fact
    fingerprints bind the returned text to the exact approved source snapshot.
    """
    if (not isinstance(profile, dict) or type(profile.get("id")) is not str
            or not profile["id"].strip() or type(expected_profile_version) is not str
            or not _SHA256.fullmatch(expected_profile_version)):
        _fail("COVER_LETTER_PREVIEW_PROFILE_REQUIRED", "A current profile snapshot is required")
    if profile.get("version") != expected_profile_version:
        _fail("COVER_LETTER_PREVIEW_PROFILE_STALE", "The profile changed; reload current approved facts")

    job_data = _job_snapshot(job)
    if (type(expected_job_fingerprint) is not str
            or not _SHA256.fullmatch(expected_job_fingerprint)
            or job_data["fingerprint"] != expected_job_fingerprint):
        _fail("COVER_LETTER_PREVIEW_JOB_STALE", "The target job changed; reload the saved job")
    _validate_job_evidence(job)

    fact_rows = _current_fact_rows(facts, profile)
    try:
        paragraphs, context = build_cover_letter(
            profile, job, list(fact_rows.values()), style=style, length=length
        )
    except PreparationError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        _fail("COVER_LETTER_PREVIEW_EVIDENCE_INVALID", "The saved preview evidence is unsupported")

    fact_ids = context.get("fact_ids")
    matched_terms = context.get("matched_terms")
    if not isinstance(fact_ids, list) or not fact_ids or not isinstance(matched_terms, dict):
        _fail("COVER_LETTER_RELEVANT_FACTS_MISSING", "No approved, sourced career evidence matches this posting")

    selected_facts = []
    source_fact_fingerprints = {}
    for fact_id in fact_ids:
        row = fact_rows.get(fact_id)
        if (row is None or row.get("verified") is not True or row.get("user_approved") is not True
                or type(row.get("confidence")) not in (int, float) or row["confidence"] < 80
                or not isinstance(row.get("source"), str) or not row["source"].strip()
                or not isinstance(row.get("text"), str) or not row["text"].strip()):
            _fail("COVER_LETTER_PREVIEW_EVIDENCE_INVALID", "A selected career fact is no longer eligible")
        fingerprint = tailor.source_fact_fingerprint(row)
        source_fact_fingerprints[fact_id] = fingerprint
        selected_facts.append({
            "fact_id": fact_id,
            "kind": row.get("kind"),
            "source": row["source"],
            "verified": True,
            "user_approved": True,
            "confidence": row["confidence"],
            "sha256": fingerprint,
            "matched_terms": list(matched_terms.get(fact_id, [])),
        })

    attributes = job.get("attributes") if isinstance(job.get("attributes"), dict) else {}
    approved_context = _approved_company_context(job)
    if approved_context:
        safe_context_fields = (
            "content_origin", "content_status", "content", "source_url", "source_host",
            "verified_domain", "source_identity_method", "identity_status", "content_sha256",
            "version", "reviewed_at", "user_reviewed", "user_approved",
        )
        company_context = {
            **{key: approved_context[key] for key in safe_context_fields},
            "included": True,
            "caveat": "USER_PROVIDED / CONTENT UNVERIFIED; source identity is verified and you reviewed and approved this context.",
        }
        company_context_evidence = {
            "included": True,
            "status": "USER_PROVIDED_CONTENT_UNVERIFIED",
            "content_sha256": approved_context["content_sha256"],
            "version": approved_context["version"],
            "caveat": company_context["caveat"],
        }
        company_context_caveat = company_context["caveat"]
    else:
        company_context = None
        company_context_caveat = (
            "USER_PROVIDED / CONTENT UNVERIFIED company context is omitted unless saved review and source evidence are valid."
        )
        company_context_evidence = {
            "included": False,
            "status": "OMITTED_UNVERIFIED",
            "caveat": company_context_caveat,
        }

    company_identity = context.get("company_identity")
    if not isinstance(company_identity, str) or not company_identity.strip() or len(company_identity) > 128:
        company_identity = "SOURCE_LISTED"
    skills = list(context["skills"])
    skill_strengths = {
        skill: context["skill_strengths"].get(skill, "UNKNOWN") for skill in skills
    }
    responsibilities = list(context["responsibilities"])
    responsibility_source = (
        "SAVED_RESPONSIBILITY_EVIDENCE" if job.get("responsibility_evidence")
        else "SAVED_DESCRIPTION_PARSED"
    )
    raw_skill_source = attributes.get("skill_evidence")
    skill_source = (
        raw_skill_source.strip()[:128]
        if isinstance(raw_skill_source, str) and raw_skill_source.strip()
        else "SAVED_JOB_SKILLS_UNCLASSIFIED"
    )
    job_evidence = {
        "skills": [
            {"term": skill, "strength": skill_strengths[skill]} for skill in skills
        ],
        "skill_source": skill_source,
        "responsibilities": responsibilities,
        "responsibility_source": responsibility_source,
    }

    preview_text = "\n\n".join(paragraphs)
    if not preview_text.strip() or len(preview_text) > 20000:
        _fail("COVER_LETTER_PREVIEW_RESULT_INVALID", "The deterministic preview is not within supported bounds")

    return {
        "schema_version": 1,
        "status": "READY",
        "preview_text": preview_text,
        "profile_id": profile["id"],
        "profile_version": expected_profile_version,
        "job_id": job["id"],
        "job_fingerprint": job_data["fingerprint"],
        "fact_ids": list(fact_ids),
        "source_fact_ids": list(fact_ids),
        "source_fact_fingerprints": source_fact_fingerprints,
        "source_facts": selected_facts,
        "matched_terms": {fact_id: list(matched_terms[fact_id]) for fact_id in fact_ids},
        "responsibilities": responsibilities,
        "skills": skills,
        "skill_strengths": skill_strengths,
        "company_identity": company_identity,
        "company_context": company_context,
        "company_context_evidence": company_context_evidence,
        "company_context_caveat": company_context_caveat,
        "job_evidence": job_evidence,
        "style": context["style"],
        "length": context["length"],
        "read_only": True,
        "document_created": False,
    }
