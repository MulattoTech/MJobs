"""Read-only acceptance proof for job-specific generated cover-letter artifacts."""
from __future__ import annotations

import hashlib
from io import BytesIO
import re

from docx import Document
from pypdf import PdfReader


def _require(condition: bool, message: str) -> None:
    """Keep acceptance guards active even when Python runs with ``-O``."""
    if not condition:
        raise AssertionError(message)


def prove_unique_cover_letter_artifacts(
    get_json,
    get_bytes,
    *,
    profile_id: str,
    cases: dict[str, dict],
    base_revision_id: str,
    base_sha256: str,
    base_bytes: bytes,
    approved_by_case: dict[str, tuple[str, ...]],
    pending_sentinels: tuple[str, ...],
) -> dict:
    """Prove two persisted letters are job-bound, truthful, unique and recoverable.

    ``get_json`` and ``get_bytes`` read public application APIs. Invoke this
    against the reopened service to make its returned evidence a post-restart
    readback. ``cases`` maps labels (for example A/B) to the expected
    application_id, job_id, title, company, cover-letter id and version.
    ``base_bytes`` is the exact upload captured before either application was
    prepared.
    """
    _require(len(cases) == 2, "uniqueness proof requires exactly two job contexts")
    _require(set(cases) == set(approved_by_case),
             "approved facts must be declared for each job context")
    base_path = f"/profiles/{profile_id}/document-library/{base_revision_id}"
    _require(hashlib.sha256(base_bytes).hexdigest() == base_sha256,
             "uploaded base resume hash does not match its recorded digest")
    _require(get_bytes(base_path) == base_bytes, "uploaded base resume changed")
    library = get_json(f"/profiles/{profile_id}/document-library")
    base_row = next(
        (row for row in library["uploaded"] if row["revision_id"] == base_revision_id),
        None,
    )
    _require(base_row is not None, "original uploaded revision disappeared")
    _require(base_row["sha256"] == base_sha256,
             "original uploaded revision digest changed")

    evidence = {}
    for label, expected in cases.items():
        application_id = expected["application_id"]
        application = get_json(
            f"/profiles/{profile_id}/applications/{application_id}"
        )
        _require(application["id"] == application_id,
                 f"{label}: application identity changed")
        _require(application["profile_id"] == profile_id,
                 f"{label}: application profile changed")
        _require(application["job_id"] == expected["job_id"],
                 f"{label}: application job changed")

        job = get_json(f"/jobs/{expected['job_id']}?profile_id={profile_id}")
        _require(job["id"] == expected["job_id"], f"{label}: job identity changed")
        _require(job["title"] == expected["title"], f"{label}: job title changed")
        _require(job["company"] == expected["company"], f"{label}: job company changed")

        payload = get_json(
            f"/profiles/{profile_id}/applications/{application_id}/documents"
        )
        rows = [row for row in payload["documents"] if row["kind"] == "cover-letter"]
        letter = next(
            (row for row in rows if row["id"] == expected["document_id"]), None
        )
        _require(letter is not None,
                 f"{label}: expected cover-letter revision is missing")
        _require(letter["version"] == expected["version"],
                 f"{label}: cover-letter version changed")
        _require(letter["profile_id"] == profile_id,
                 f"{label}: cover-letter profile changed")
        _require(letter["job_id"] == expected["job_id"],
                 f"{label}: cover-letter job changed")
        _require(letter["profile_version"] == application["profile_version"],
                 f"{label}: cover-letter profile version changed")
        _require(letter["fact_ids"] == expected["fact_ids"],
                 f"{label}: cover-letter fact selection changed")
        _require({row["fact_id"] for row in letter["facts_used"]}
                 == set(expected["fact_ids"],),
                 f"{label}: cover-letter facts used changed")
        _require(letter["preview_error"] is None,
                 f"{label}: cover-letter preview failed")
        _require(bool(letter["preview_text"]),
                 f"{label}: cover-letter preview is empty")

        extracted = {}
        hashes = {}
        for format in ("txt", "pdf", "docx"):
            data = get_bytes(f"/profiles/{profile_id}/documents/{letter['id']}/{format}")
            digest = hashlib.sha256(data).hexdigest()
            _require(digest == letter["sha256"][format],
                     f"{label}: {format} hash mismatch")
            hashes[format] = digest
            if format == "txt":
                text = data.decode("utf-8")
            elif format == "pdf":
                text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
            else:
                text = "\n".join(row.text for row in Document(BytesIO(data)).paragraphs)
            normalized = re.sub(r"\s+", " ", text)
            _require(f"Application for {expected['title']} at {expected['company']}"
                     in normalized, f"{label}: {format} lacks job-specific context")
            if expected.get("responsibility"):
                _require(expected["responsibility"] in normalized,
                         f"{label}: {format} lacks the expected responsibility")
            for fact in approved_by_case[label]:
                _require(fact in normalized,
                         f"{label}: approved fact missing from {format}")
            for sentinel in pending_sentinels:
                _require(sentinel not in normalized,
                         f"{label}: pending fact leaked into {format}")
            extracted[format] = text

        _require(letter["preview_text"].strip() == extracted["txt"].strip(),
                 f"{label}: saved preview differs from the text artifact")
        evidence[label] = {
            "application_id": application_id,
            "profile_id": profile_id,
            "job_id": expected["job_id"],
            "profile_version": letter["profile_version"],
            "document_id": letter["id"],
            "version": letter["version"],
            "sha256": hashes,
        }

    labels = list(cases)
    first, second = evidence[labels[0]], evidence[labels[1]]
    _require(first["job_id"] != second["job_id"],
             "cases must target different jobs")
    _require(first["application_id"] != second["application_id"],
             "distinct jobs must have distinct applications")
    _require(first["document_id"] != second["document_id"],
             "distinct jobs must have distinct cover-letter documents")
    _require(first["sha256"]["txt"] != second["sha256"]["txt"],
             "distinct job contexts produced byte-identical cover letters")
    evidence["base_upload"] = {
        "revision_id": base_revision_id,
        "sha256": base_sha256,
        "unchanged": True,
    }
    return evidence
