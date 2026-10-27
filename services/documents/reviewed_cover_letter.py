"""Materialize an explicitly approved cover-letter preview for one application."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
from uuid import uuid4

from docx import Document
from docx.shared import Pt
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.documents.artifacts import atomic_generate, publish_directory
from services.documents.cover_letter import COVER_LETTER_TEMPLATE, build_cover_letter
from services.documents.failure_status import validate_generated_outputs
from services.documents.render import render_pdf
from services.documents.validation import validate_assertions
from services.documents.cover_letter_preview import cover_letter_preview_fingerprint


class ReviewedCoverLetterError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def materialize_reviewed_preview(
    store,
    output_dir: str | Path,
    *,
    profile: dict,
    job: dict,
    application_id: str,
    expected_base_document_version: int,
    preview: dict,
    preview_sha256: str,
    request_id: str,
) -> dict:
    """Persist one immutable future-version row without promoting the application."""
    if hashlib.sha256(preview["preview_text"].encode("utf-8")).hexdigest() != preview_sha256:
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_PREVIEW_MISMATCH")
    application = store.application(application_id)
    if application["profile_id"] != profile["id"] or application["job_id"] != job["id"]:
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_SCOPE_MISMATCH")
    if application["document_version"] != expected_base_document_version:
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_BASE_STALE")

    target_version = expected_base_document_version + 1
    document_id = identity(
        "reviewed-cover-letter", application_id,
        expected_base_document_version, target_version,
    )
    paragraphs, generated_context = build_cover_letter(
        profile, job, profile["facts"], preview["style"], preview["length"],
    )
    if "\n\n".join(paragraphs) != preview["preview_text"]:
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_PREVIEW_MISMATCH")
    context = {
        **generated_context,
        "review_status": "APPROVED",
        "approval_request_id": request_id,
        "reviewed_preview_sha256": preview_sha256,
        "base_document_version": expected_base_document_version,
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "job_id": job["id"],
        "job_fingerprint": preview["job_fingerprint"],
        "style": preview["style"],
        "length": preview["length"],
        "job_snapshot": job,
        "source_fact_fingerprints": preview["source_fact_fingerprints"],
    }
    claim_validation = validate_assertions(
        "cover-letter", paragraphs, profile, generated_context["fact_ids"], job["title"],
        COVER_LETTER_TEMPLATE, generation_context=context,
    )
    context["claim_validation"] = claim_validation

    output_root = Path(output_dir).resolve()
    application_root = (output_root / application_id).resolve()
    if not application_root.is_relative_to(output_root):
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_PATH_INVALID")
    relative = f"{profile['version']}/reviewed-{document_id}"
    destination = (application_root / relative).resolve()
    if not destination.is_relative_to(application_root):
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_PATH_INVALID")
    staging = destination.with_name(f".{destination.name}.staging-{uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    published = False
    try:
        document = Document()
        document.styles["Normal"].font.name = "Arial"
        document.styles["Normal"].font.size = Pt(11)
        for text in paragraphs:
            document.add_paragraph(text)
        paths = {
            "docx": staging / "cover-letter.docx",
            "pdf": staging / "cover-letter.pdf",
            "txt": staging / "cover-letter.txt",
        }
        atomic_generate(paths["docx"], document.save)
        atomic_generate(paths["txt"], lambda path: path.write_text(
            "\n".join(paragraphs) + "\n", encoding="utf-8", newline="\n",
        ))
        atomic_generate(paths["pdf"], lambda path: render_pdf(path, paragraphs))
        validate_generated_outputs(paths)
        record = {
            "id": document_id,
            "application_id": application_id,
            "kind": "cover-letter",
            "version": target_version,
            "profile_version": profile["version"],
            "files": {kind: f"{relative}/{path.name}" for kind, path in paths.items()},
            "fact_ids": generated_context["fact_ids"],
            "sha256": {kind: hashlib.sha256(path.read_bytes()).hexdigest()
                       for kind, path in paths.items()},
            "template": COVER_LETTER_TEMPLATE,
            "created_at": now(),
            "generation_model": "deterministic-reviewed-preview-v1",
            "generation_context": context,
        }
        with store.engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            # Match prepare(): profile, job, then application. Opposite row-lock
            # order would deadlock if approval overlaps a reprepare transaction.
            current_profile_version = connection.execute(
                select(m.profiles.c.version).where(
                    m.profiles.c.id == profile["id"],
                ).with_for_update(),
            ).scalar_one_or_none()
            current_job = connection.execute(
                select(m.jobs, m.companies.c.name.label("company")).select_from(
                    m.jobs.join(m.companies, m.jobs.c.company_id == m.companies.c.id)
                ).where(m.jobs.c.id == job["id"]).with_for_update(),
            ).mappings().one_or_none()
            locked = dict(connection.execute(select(m.applications).where(
                m.applications.c.id == application_id,
            ).with_for_update()).mappings().one())
            if (locked["profile_id"] != profile["id"] or locked["job_id"] != job["id"]):
                raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_SCOPE_MISMATCH")
            if locked["document_version"] != expected_base_document_version:
                raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_BASE_STALE")
            if (current_profile_version != profile["version"]
                    or current_job is None
                    or cover_letter_preview_fingerprint(dict(current_job))
                    != preview["job_fingerprint"]):
                raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_CONTEXT_STALE")
            existing = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == application_id,
                m.documents.c.version == target_version,
                m.documents.c.kind == "cover-letter",
            )).mappings())
            if existing:
                if len(existing) != 1 or existing[0]["id"] != document_id:
                    raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_VERSION_CONFLICT")
                saved = dict(existing[0])
                saved_context = saved.get("generation_context") or {}
                if (saved_context.get("approval_request_id") != request_id
                        or saved_context.get("reviewed_preview_sha256") != preview_sha256):
                    raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_VERSION_CONFLICT")
                shutil.rmtree(staging)
                return _result(saved, replayed=True)
            publish_directory(staging, destination)
            published = True
            connection.execute(m.documents.insert().values(**record))
        return _result(record, replayed=False)
    except Exception:
        if published:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _result(record: dict, *, replayed: bool) -> dict:
    context = record["generation_context"]
    return {
        "application_id": record["application_id"],
        "document_id": record["id"],
        "base_document_version": context["base_document_version"],
        "version": record["version"],
        "preview_sha256": context["reviewed_preview_sha256"],
        "sha256": record["sha256"],
        "replayed": replayed,
    }


def resolve_reviewed_cover_letter(
    connection,
    output_dir: str | Path,
    *,
    application: dict,
    profile: dict,
    job: dict,
    document_id: str,
    preview_sha256: str,
) -> dict:
    """Resolve and re-hash the exact approved future-version document row."""
    rows = list(connection.execute(select(m.documents).where(
        m.documents.c.application_id == application["id"],
        m.documents.c.version == application["document_version"] + 1,
        m.documents.c.kind == "cover-letter",
    )).mappings())
    if len(rows) != 1 or rows[0]["id"] != document_id:
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_SELECTION_INVALID")
    row = dict(rows[0])
    context = row.get("generation_context") or {}
    if (context.get("review_status") != "APPROVED"
            or context.get("base_document_version") != application["document_version"]
            or context.get("profile_id") != application["profile_id"]
            or context.get("profile_version") != profile["version"]
            or context.get("job_id") != application["job_id"]
            or context.get("job_fingerprint") != cover_letter_preview_fingerprint(job)
            or context.get("reviewed_preview_sha256") != preview_sha256):
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_SELECTION_STALE")
    root = Path(output_dir).resolve()
    application_root = (root / application["id"]).resolve()
    if not application_root.is_relative_to(root):
        raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_PATH_INVALID")
    for extension in ("docx", "pdf", "txt"):
        relative = row.get("files", {}).get(extension)
        path = (application_root / relative).resolve() if isinstance(relative, str) else root
        if (not path.is_relative_to(application_root) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest()
                != row.get("sha256", {}).get(extension)):
            raise ReviewedCoverLetterError("COVER_LETTER_REVIEW_CONTENT_CHANGED")
    return row
