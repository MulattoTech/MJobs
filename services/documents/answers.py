"""Deterministic, explicitly reviewed application answer exports."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
from threading import Lock
from uuid import uuid4

from sqlalchemy import func, select, text

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.documents.avoid_list import validate_content as validate_avoid_list


_SENSITIVE_TERMS = re.compile(
    r"\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|"
    r"date of birth|birth date|age|marital|citizenship|citizen|authoriz\w*|work authorization|"
    r"eligible to work|work eligibility|right to work|"
    r"visa|sponsorship|criminal|convict\w*|felon\w*|background check|legally authorized|"
    r"salary expectation\w*|salary range|desired compensation|compensation expectation\w*|"
    r"non[ -]?compete|conflict\w* of interest|minimum age|attest\w*|certif\w* under penalty)\b", re.IGNORECASE)
_QUESTION_NEGATION = re.compile(
    r"\b(?:no|not|never|without|neither|nor|cannot|unavailable|"
    r"can['’]t|won['’]t|wouldn['’]t|shouldn['’]t|couldn['’]t|"
    r"didn['’]t|doesn['’]t|don['’]t|isn['’]t|aren['’]t|"
    r"wasn['’]t|weren['’]t|haven['’]t|hasn['’]t|hadn['’]t|mustn['’]t)\b",
    re.IGNORECASE,
)

_EXPORT_LOCKS_GUARD = Lock()
_EXPORT_LOCKS = {}

DEFAULT_ANSWER_CONFIDENCE_POLICY = {
    "minimum_saved_answer_confidence": 80,
    "minimum_question_match_confidence": 0.75,
}


def validate_answer_confidence_policy(value):
    fields = set(DEFAULT_ANSWER_CONFIDENCE_POLICY)
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("ANSWER_CONFIDENCE_POLICY_INVALID")
    answer_confidence = value["minimum_saved_answer_confidence"]
    match_confidence = value["minimum_question_match_confidence"]
    if (type(answer_confidence) is not int or not 80 <= answer_confidence <= 100
            or isinstance(match_confidence, bool)
            or not isinstance(match_confidence, (int, float))
            or not 0.75 <= match_confidence <= 1):
        raise ValueError("ANSWER_CONFIDENCE_POLICY_INVALID")
    return {
        "minimum_saved_answer_confidence": answer_confidence,
        "minimum_question_match_confidence": float(match_confidence),
    }


def get_answer_confidence_policy(store, profile_id, connection=None):
    """Return one career profile's durable thresholds or behavior-preserving defaults."""
    if connection is None:
        store.profile(profile_id)
        with store.engine.connect() as current:
            return get_answer_confidence_policy(store, profile_id, current)
    row = connection.execute(select(m.answer_confidence_policies).where(
        m.answer_confidence_policies.c.profile_id == profile_id)).mappings().one_or_none()
    if row is None:
        return dict(DEFAULT_ANSWER_CONFIDENCE_POLICY)
    return {
        "minimum_saved_answer_confidence": row["minimum_saved_answer_confidence"],
        "minimum_question_match_confidence": row["minimum_question_match_confidence"],
    }


def save_answer_confidence_policy(store, profile_id, value):
    """Persist thresholds without changing profile content or answer approvals."""
    policy = validate_answer_confidence_policy(value)
    store.profile(profile_id)
    with store.engine.begin() as connection:
        existing = connection.execute(select(m.answer_confidence_policies).where(
            m.answer_confidence_policies.c.profile_id == profile_id).with_for_update()
        ).mappings().one_or_none()
        current = ({
            "minimum_saved_answer_confidence": existing["minimum_saved_answer_confidence"],
            "minimum_question_match_confidence": existing["minimum_question_match_confidence"],
        } if existing else dict(DEFAULT_ANSWER_CONFIDENCE_POLICY))
        upsert(connection, m.answer_confidence_policies, {
            "id": identity(profile_id, "answer-confidence-policy"),
            "profile_id": profile_id,
            **policy,
            "updated_at": now(),
        }, replace=True)
        if policy != current:
            store._audit(connection, profile_id, "SETTING_CHANGED", {})
    return policy


def _export_lock(application_id):
    # SQLite ignores row-level locks, so serialize same-process exports there.
    # PostgreSQL also takes an advisory transaction lock to cover other workers.
    with _EXPORT_LOCKS_GUARD:
        return _EXPORT_LOCKS.setdefault(application_id, Lock())


def normalize_question(value):
    # Keep the ASCII token behavior identical to the browser matcher.
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


_SEMANTIC_QUESTION_REWRITES = (
    # Curate only this ordinary scheduling pair. Weekday availability and
    # sensitive/legal questions stay outside this equivalence.
    (re.compile(r"\b(are you available to work weekends|can you work (?:on )?weekends)\b", re.IGNORECASE),
     "can you work weekends"),
    (re.compile(r"\b(coding|software development)\s+languages?\b", re.IGNORECASE),
     "programming languages"),
    # "Provider" and "platform" are interchangeable here only as part of the
    # ordinary cloud experience prompt. This bounded rewrite is not a general
    # semantic model; sensitive and legal prompts remain excluded separately.
    (re.compile(r"\bcloud providers\b", re.IGNORECASE), "cloud platforms"),
    # Expand one standard IT operations acronym; do not equate adjacent fields
    # such as service desk or service delivery with service management.
    (re.compile(r"\bitsm\b", re.IGNORECASE), "it service management"),
    (re.compile(r"\b(what|which)\b", re.IGNORECASE), "which"),
    (re.compile(r"\bworked with\b", re.IGNORECASE), "used"),
)


def _semantic_question_key(value):
    canonical = value
    for pattern, replacement in _SEMANTIC_QUESTION_REWRITES:
        canonical = pattern.sub(replacement, canonical)
    return normalize_question(canonical)


def _question_is_negated(value):
    """Conservatively detect common explicit negation words and contractions."""
    return bool(_QUESTION_NEGATION.search(value))


def _token_edit_distance(left, right):
    """Levenshtein distance over bounded question tokens, using linear memory."""
    previous = list(range(len(right) + 1))
    for row, left_token in enumerate(left, start=1):
        current = [row]
        for column, right_token in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_token != right_token),
            ))
        previous = current
    return previous[-1]


def match_answer_question(question, saved_question, minimum_confidence=0.75):
    """Return a transparent local question match, or None when no safe match exists.

    The semantic tier covers only the explicit synonym rewrites above. It is not
    a general language model and does not classify answer meaning or sensitivity.
    """
    if (isinstance(minimum_confidence, bool)
            or not isinstance(minimum_confidence, (int, float))
            or not 0.75 <= minimum_confidence <= 1):
        raise ValueError("ANSWER_CONFIDENCE_POLICY_INVALID")
    if not isinstance(question, str) or not isinstance(saved_question, str):
        return None
    if _SENSITIVE_TERMS.search(question) or _SENSITIVE_TERMS.search(saved_question):
        return None
    normalized = normalize_question(question)
    saved_normalized = normalize_question(saved_question)
    if not normalized or not saved_normalized:
        return None
    # Edit distance can make a one-token polarity reversal look highly similar.
    # Keep exact wording reusable, but never infer across opposite polarity.
    if _question_is_negated(question) != _question_is_negated(saved_question):
        return None
    if normalized == saved_normalized:
        score = 1.0
        return {"method": "EXACT_NORMALIZATION", "score": score} if score >= minimum_confidence else None
    if _semantic_question_key(question) == _semantic_question_key(saved_question):
        score = 0.95
        return {"method": "CURATED_SEMANTIC", "score": score} if score >= minimum_confidence else None

    left = normalized.split()
    right = saved_normalized.split()
    if max(len(left), len(right)) > 40 or min(len(left), len(right)) < 5:
        return None
    overlap = len(set(left) & set(right)) / len(set(left) | set(right))
    score = round(1 - _token_edit_distance(left, right) / max(len(left), len(right)), 3)
    if overlap < 0.7 or score < minimum_confidence:
        return None
    return {"method": "FUZZY", "score": score}


def export_answers(store, application_id, output_dir, items):
    root = Path(output_dir).resolve()
    application_root = (root / application_id).resolve()
    if not application_root.is_relative_to(root):
        raise ValueError("Application output path is invalid")
    application_root.mkdir(parents=True, exist_ok=True)
    with _export_lock(application_id):
        with store.engine.begin() as connection:
            if store.engine.dialect.name == "sqlite":
                # SQLite has no row locks. Reserve the writer before reading the
                # approval snapshot so profile updates cannot overtake the export.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            if store.engine.dialect.name == "postgresql":
                lock_key = int.from_bytes(hashlib.sha256(
                    f"mjobs-answer-export:{application_id}".encode("utf-8")).digest()[:8],
                    byteorder="big", signed=True)
                connection.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"),
                                   {"lock_key": lock_key})
            application = connection.execute(select(m.applications).where(
                m.applications.c.id == application_id)).mappings().one_or_none()
            if application is None:
                raise ValueError("Application is unavailable")
            profile_id = application["profile_id"]
            profile_lock = select(m.profiles.c.id).where(m.profiles.c.id == profile_id)
            if store.engine.dialect.name == "postgresql":
                profile_lock = profile_lock.with_for_update()
            if connection.execute(profile_lock).scalar_one_or_none() is None:
                raise ValueError("Application profile is unavailable")
            # Keep approval eligibility and the immutable document record inside
            # the same transaction as the profile-row lock. A substantive profile
            # edit must wait until this export commits.
            profile = store.profile(profile_id, connection=connection)
            policy = get_answer_confidence_policy(store, profile_id, connection=connection)
            bank = {row["id"]: row for row in profile.get("answers", [])}
            used_questions = set()
            exported = []
            manifest = []
            for item in items:
                question = item.question.strip()
                answer = item.text.strip()
                normalized = normalize_question(question)
                if not normalized or normalized in used_questions:
                    raise ValueError("Each application question must be present once")
                used_questions.add(normalized)
                answer_id = item.answer_id
                saved_question = None
                match = None
                if answer_id:
                    reusable = bank.get(answer_id)
                    if reusable is None or not reusable.get("question"):
                        raise ValueError("Reusable answer is unavailable in this application profile snapshot")
                    if _SENSITIVE_TERMS.search(question) or _SENSITIVE_TERMS.search(reusable["question"]):
                        raise ValueError("Sensitive or legal questions require an answer entered for this application")
                    match = match_answer_question(
                        question, reusable["question"],
                        minimum_confidence=policy["minimum_question_match_confidence"])
                    if match is None:
                        raise ValueError("Reusable answer question did not meet a supported match threshold")
                    if (not reusable["verified"] or not reusable["user_approved"]
                            or reusable["confidence"] < policy["minimum_saved_answer_confidence"]):
                        raise ValueError("Reusable answer needs deliberate verification and approval first")
                    if reusable["text"].strip() != answer:
                        raise ValueError("Review the saved answer text before exporting it")
                    source = reusable["source"]
                    confidence = reusable["confidence"]
                    saved_question = reusable["question"]
                else:
                    source = "Entered and reviewed for this application"
                    confidence = None
                validate_avoid_list(answer, profile.get('preferences'),
                                    f'application answer {len(manifest) + 1}')
                exported.extend((f"Question: {question}", f"Answer: {answer}", ""))
                manifest.append({"question": question, "answer_id": answer_id,
                                 "source": source, "confidence": confidence,
                                 "minimum_saved_answer_confidence": policy["minimum_saved_answer_confidence"],
                                 "minimum_question_match_confidence": policy["minimum_question_match_confidence"],
                                 "answer_mode": "REVIEWED_REUSE" if answer_id else "USER_ENTERED",
                                 "saved_question": saved_question,
                                 "match_method": match["method"] if match else None,
                                 "match_score": match["score"] if match else None})

            content = ("\n".join(exported).rstrip() + "\n").encode("utf-8")
            latest = connection.execute(select(func.max(m.documents.c.version)).where(
                m.documents.c.application_id == application_id,
                m.documents.c.kind == "application-answer-export")).scalar_one_or_none()
            version = (latest or 0) + 1
            document_id = identity(application_id, "application-answer-export", version)
            digest = hashlib.sha256(content).hexdigest()
            relative = f"answers/{version}/{document_id}-{digest}.txt"
            path = (application_root / relative).resolve()
            if not path.is_relative_to(application_root):
                raise ValueError("Answer export storage path is invalid")
            path.parent.mkdir(parents=True, exist_ok=True)
            # Content-address the immutable path so a crash after file creation
            # but before the DB commit cannot poison a later attempt at this
            # version. Reuse only byte-identical output at the same digest path.
            if path.exists():
                existing = path.read_bytes()
                if existing != content or hashlib.sha256(existing).hexdigest() != digest:
                    raise ValueError("Answer export path contains different immutable bytes")
            else:
                # Write to a unique same-directory temporary file and atomically
                # promote it only after all bytes are present. A crash during the
                # write leaves no partial file at the immutable revision path.
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                try:
                    with temporary.open("xb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            record = {"id": document_id, "application_id": application_id,
                      "kind": "application-answer-export", "version": version,
                      "profile_version": profile["version"], "files": {"txt": relative},
                      "fact_ids": [], "sha256": {"txt": digest},
                      "template": "reviewed-application-answers-v1",
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "generation_model": "manual-reviewed-answers-v1",
                      "generation_context": {"mode": "REVIEWED_APPLICATION_ANSWERS",
                          "application_id": application_id, "profile_id": application["profile_id"],
                          "profile_version": profile["version"], "items": manifest}}
            try:
                upsert(connection, m.documents, record)
                store._audit(connection, application_id, "APPLICATION_ANSWERS_EXPORTED",
                              {"document_id": document_id, "version": version,
                               "sha256": digest, "item_count": len(manifest)})
            except Exception:
                path.unlink(missing_ok=True)
                raise
    return record
