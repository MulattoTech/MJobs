"""Deterministic, explicitly reviewed application answer exports."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from threading import Lock

from sqlalchemy import func, select, text

from packages.persistence import models as m
from packages.persistence.store import identity, upsert


_SENSITIVE_TERMS = re.compile(
    r"\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|"
    r"date of birth|birth date|age|marital|citizenship|citizen|authoriz\w*|work authorization|"
    r"eligible to work|work eligibility|right to work|"
    r"visa|sponsorship|criminal|convict\w*|felon\w*|background check|legally authorized|"
    r"salary expectation\w*|salary range|desired compensation|compensation expectation\w*|"
    r"non[ -]?compete|conflict of interest|minimum age|attest\w*|certif\w* under penalty)\b", re.IGNORECASE)

_EXPORT_LOCKS_GUARD = Lock()
_EXPORT_LOCKS = {}


def _export_lock(application_id):
    # SQLite ignores row-level locks, so serialize same-process exports there.
    # PostgreSQL also takes an advisory transaction lock to cover other workers.
    with _EXPORT_LOCKS_GUARD:
        return _EXPORT_LOCKS.setdefault(application_id, Lock())


def normalize_question(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def export_answers(store, application_id, output_dir, items):
    application = store.application(application_id)
    # Application preparation and reusable-answer review can occur at
    # different times; bind the export to the exact profile version used now.
    profile = store.profile(application["profile_id"])
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
        if answer_id:
            reusable = bank.get(answer_id)
            if reusable is None or not reusable.get("question"):
                raise ValueError("Reusable answer is unavailable in this application profile snapshot")
            if normalize_question(reusable["question"]) != normalized:
                raise ValueError("Reusable answer question does not match this application question")
            if _SENSITIVE_TERMS.search(question):
                raise ValueError("Sensitive or legal questions require an answer entered for this application")
            if not reusable["verified"] or not reusable["user_approved"] or reusable["confidence"] < 80:
                raise ValueError("Reusable answer needs deliberate verification and approval first")
            if reusable["text"].strip() != answer:
                raise ValueError("Review the saved answer text before exporting it")
            source = reusable["source"]
            confidence = reusable["confidence"]
        else:
            source = "Entered and reviewed for this application"
            confidence = None
        exported.extend((f"Question: {question}", f"Answer: {answer}", ""))
        manifest.append({"question": question, "answer_id": answer_id,
                         "source": source, "confidence": confidence,
                         "answer_mode": "REVIEWED_REUSE" if answer_id else "USER_ENTERED"})

    content = ("\n".join(exported).rstrip() + "\n").encode("utf-8")
    root = Path(output_dir).resolve()
    application_root = (root / application_id).resolve()
    if not application_root.is_relative_to(root):
        raise ValueError("Application output path is invalid")
    application_root.mkdir(parents=True, exist_ok=True)
    with _export_lock(application_id):
        with store.engine.begin() as connection:
            if store.engine.dialect.name == "postgresql":
                lock_key = int.from_bytes(hashlib.sha256(
                    f"mjobs-answer-export:{application_id}".encode("utf-8")).digest()[:8],
                    byteorder="big", signed=True)
                connection.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"),
                                   {"lock_key": lock_key})
            latest = connection.execute(select(func.max(m.documents.c.version)).where(
                m.documents.c.application_id == application_id,
                m.documents.c.kind == "application-answer-export")).scalar_one_or_none()
            version = (latest or 0) + 1
            document_id = identity(application_id, "application-answer-export", version)
            relative = f"answers/{version}/{document_id}.txt"
            path = (application_root / relative).resolve()
            if not path.is_relative_to(application_root):
                raise ValueError("Answer export storage path is invalid")
            path.parent.mkdir(parents=True, exist_ok=True)
            # IDs are version-derived and immutable; refuse to overwrite a prior file.
            with path.open("xb") as output:
                output.write(content)
            digest = hashlib.sha256(content).hexdigest()
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
