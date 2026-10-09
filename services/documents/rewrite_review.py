"""Read-only evidence previews for proposed resume-fact wording changes."""
from __future__ import annotations

from collections import Counter
import hashlib

from services.documents import tailor
from services.documents.errors import PreparationError
from services.documents.validation import _DATE_TOKEN, _NUMBER_TOKEN


_MAX_REWRITES = 100
_MAX_FACT_ID_LENGTH = 128


def _fail(code: str, message: str) -> None:
    raise PreparationError(code, message)


def _fingerprint(value) -> bool:
    return (type(value) is str and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _validate_snapshots(profile: dict, job: dict,
                        expected_profile_version: str,
                        expected_job_fingerprint: str) -> str:
    if not isinstance(profile, dict) or not _fingerprint(expected_profile_version):
        _fail("RESUME_REWRITE_PROFILE_STALE", "Reload the current profile before previewing rewrites")
    if profile.get("version") != expected_profile_version:
        _fail("RESUME_REWRITE_PROFILE_STALE", "Profile changed; reload approved facts before previewing")
    if not isinstance(job, dict) or type(job.get("id")) is not str or not job["id"]:
        _fail("RESUME_REWRITE_JOB_INVALID", "A saved target job is required")
    if not _fingerprint(expected_job_fingerprint):
        _fail("RESUME_REWRITE_JOB_STALE", "Reload the current target job before previewing rewrites")
    try:
        current_job_fingerprint = tailor.target_fingerprint(job)
    except (KeyError, TypeError, ValueError):
        _fail("RESUME_REWRITE_JOB_INVALID", "Target job snapshot is unsupported")
    if current_job_fingerprint != expected_job_fingerprint:
        _fail("RESUME_REWRITE_JOB_STALE", "Target job changed; reload it before previewing")
    return current_job_fingerprint


def _validate_selection(profile: dict, selected_fact_ids, rewrites) -> tuple[list[str], dict[str, str], dict[str, dict]]:
    if (type(selected_fact_ids) is not list or not 1 <= len(selected_fact_ids) <= _MAX_REWRITES
            or any(type(fact_id) is not str or not fact_id.strip()
                   or len(fact_id) > _MAX_FACT_ID_LENGTH for fact_id in selected_fact_ids)
            or len(set(selected_fact_ids)) != len(selected_fact_ids)
            or type(rewrites) is not list or not 1 <= len(rewrites) <= _MAX_REWRITES):
        _fail("RESUME_REWRITE_SELECTION_INVALID", "Select unique approved resume facts and provide one proposal for each")

    proposals = {}
    for item in rewrites:
        if (type(item) is not dict or set(item) != {"fact_id", "text"}
                or type(item.get("fact_id")) is not str or type(item.get("text")) is not str):
            _fail("RESUME_REWRITE_INVALID", "Each proposal must provide a fact ID and proposed wording")
        fact_id = item["fact_id"]
        if (not fact_id.strip() or len(fact_id) > _MAX_FACT_ID_LENGTH
                or fact_id in proposals):
            _fail("RESUME_REWRITE_SELECTION_INVALID", "Rewrite proposals must reference unique selected facts")
        proposals[fact_id] = item["text"]
    if set(proposals) != set(selected_fact_ids):
        _fail("RESUME_REWRITE_SELECTION_INVALID", "Provide exactly one rewrite proposal for each selected fact")

    index: dict[str, list[dict]] = {}
    facts = profile.get("facts")
    if type(facts) is not list:
        _fail("RESUME_REWRITE_FACT_INVALID", "Approved profile facts are unavailable")
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_id = fact.get("fact_id", fact.get("id"))
        if type(fact_id) is str:
            index.setdefault(fact_id, []).append(fact)

    eligible = {}
    for fact_id in selected_fact_ids:
        matches = index.get(fact_id, [])
        if len(matches) != 1:
            _fail("RESUME_REWRITE_FACT_INVALID", "A selected resume fact is missing or ambiguous")
        source = matches[0]
        # Reuse the tailoring service's fact threshold and exact allowed kinds.
        try:
            is_eligible = source in tailor.eligible_rows({"facts": [source]})
        except (KeyError, TypeError, ValueError):
            is_eligible = False
        if (not is_eligible or type(source.get("text")) is not str or not source["text"].strip()
                or source.get("kind") not in tailor.REWRITABLE_RESUME_FACT_KINDS):
            _fail("RESUME_REWRITE_FACT_INVALID", "Only current approved resume facts in supported sections may be rewritten")
        allowed_uses = source.get("allowed_uses")
        if allowed_uses is not None and (
                type(allowed_uses) is not list or "RESUME" not in allowed_uses):
            _fail("RESUME_REWRITE_FACT_INVALID", "This fact is not approved for resume rewrite use")
        eligible[fact_id] = source
    return list(selected_fact_ids), {fact_id: proposals[fact_id] for fact_id in selected_fact_ids}, eligible


def _protected_token_audit(source_text: str, proposed_text: str) -> dict:
    required = [
        *({"type": "DATE", "value": token} for token in _DATE_TOKEN.findall(source_text)),
        *({"type": "NUMBER", "value": token} for token in _NUMBER_TOKEN.findall(source_text)),
        *({"type": "NAMED_CREDENTIAL", "value": token}
          for token in tailor._named_source_tokens(source_text)),
    ]
    available = Counter()
    available.update(("DATE", token.casefold()) for token in _DATE_TOKEN.findall(proposed_text))
    available.update(("NUMBER", token.casefold()) for token in _NUMBER_TOKEN.findall(proposed_text))
    available.update(("NAMED_CREDENTIAL", token)
                     for token in tailor._named_source_tokens(proposed_text))

    retained = []
    missing = []
    for token in required:
        key = (token["type"], token["value"].casefold()
               if token["type"] in {"DATE", "NUMBER"} else token["value"])
        if available[key] > 0:
            available[key] -= 1
            retained.append(dict(token))
        else:
            missing.append(dict(token))
    return {"required": required, "retained": retained, "missing": missing}


def build_rewrite_review(profile, job, selected_fact_ids, rewrites,
                         expected_profile_version, expected_job_fingerprint) -> dict:
    """Preview explicit resume rewrite proposals without saving or preparing output.

    This checks only exact protected-token retention. It makes no semantic
    equivalence claim; the user must review every proposal and correct any
    missing dates, numbers, or credential-bearing names before preparation.
    """
    if not isinstance(profile, dict) or type(profile.get("id")) is not str or not profile["id"]:
        _fail("RESUME_REWRITE_PROFILE_STALE", "A current profile snapshot is required")
    current_job_fingerprint = _validate_snapshots(
        profile, job, expected_profile_version, expected_job_fingerprint
    )
    selected_ids, proposals, eligible = _validate_selection(
        profile, selected_fact_ids, rewrites
    )
    preview_rows = []
    for fact_id in selected_ids:
        source = eligible[fact_id]
        proposed_text = proposals[fact_id].strip()
        if (not proposed_text or len(proposed_text) > 10_000 or "\x00" in proposed_text
                or proposed_text == source["text"]):
            _fail("RESUME_REWRITE_INVALID", "Enter changed, nonempty resume wording within the supported limit")
        preview_rows.append({
            "fact_id": fact_id,
            "fact_kind": source["kind"],
            "source_text": source["text"],
            "source_sha256": hashlib.sha256(source["text"].encode("utf-8")).hexdigest(),
            "proposed_text": proposed_text,
            "protected_token_audit": _protected_token_audit(source["text"], proposed_text),
        })

    return {
        "profile_id": profile["id"],
        "profile_version": expected_profile_version,
        "job_id": job["id"],
        "job_fingerprint": current_job_fingerprint,
        "selected_fact_ids": selected_ids,
        "rewrites": preview_rows,
        "read_only": True,
        "review_required": True,
        "semantic_equivalence": "USER_REVIEW_REQUIRED",
    }
