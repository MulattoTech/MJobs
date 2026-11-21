"""Read-only acceptance proof for job-specific generated cover-letter artifacts."""
from __future__ import annotations

import hashlib
from io import BytesIO
import re

from docx import Document
from pypdf import PdfReader


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
    assert len(cases) == 2, "uniqueness proof requires exactly two job contexts"
    assert set(cases) == set(approved_by_case)
    base_path = f"/profiles/{profile_id}/document-library/{base_revision_id}"
    assert hashlib.sha256(base_bytes).hexdigest() == base_sha256
    assert get_bytes(base_path) == base_bytes, "uploaded base resume changed"
    library = get_json(f"/profiles/{profile_id}/document-library")
    base_row = next(
        (row for row in library["uploaded"] if row["revision_id"] == base_revision_id),
        None,
    )
    assert base_row is not None, "original uploaded revision disappeared"
    assert base_row["sha256"] == base_sha256

    evidence = {}
    for label, expected in cases.items():
        application_id = expected["application_id"]
        application = get_json(
            f"/profiles/{profile_id}/applications/{application_id}"
        )
        assert application["id"] == application_id
        assert application["profile_id"] == profile_id
        assert application["job_id"] == expected["job_id"]

        job = get_json(f"/jobs/{expected['job_id']}?profile_id={profile_id}")
        assert job["id"] == expected["job_id"]
        assert job["title"] == expected["title"]
        assert job["company"] == expected["company"]

        payload = get_json(
            f"/profiles/{profile_id}/applications/{application_id}/documents"
        )
        rows = [row for row in payload["documents"] if row["kind"] == "cover-letter"]
        letter = next(
            (row for row in rows if row["id"] == expected["document_id"]), None
        )
        assert letter is not None, f"{label}: expected cover-letter revision is missing"
        assert letter["version"] == expected["version"]
        assert letter["profile_id"] == profile_id
        assert letter["job_id"] == expected["job_id"]
        assert letter["profile_version"] == application["profile_version"]
        assert letter["fact_ids"] == expected["fact_ids"]
        assert {row["fact_id"] for row in letter["facts_used"]} == set(expected["fact_ids"])
        assert letter["preview_error"] is None
        assert letter["preview_text"]

        extracted = {}
        hashes = {}
        for format in ("txt", "pdf", "docx"):
            data = get_bytes(f"/profiles/{profile_id}/documents/{letter['id']}/{format}")
            digest = hashlib.sha256(data).hexdigest()
            assert digest == letter["sha256"][format], f"{label}: {format} hash mismatch"
            hashes[format] = digest
            if format == "txt":
                text = data.decode("utf-8")
            elif format == "pdf":
                text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
            else:
                text = "\n".join(row.text for row in Document(BytesIO(data)).paragraphs)
            normalized = re.sub(r"\s+", " ", text)
            assert f"Application for {expected['title']} at {expected['company']}" in normalized
            if expected.get("responsibility"):
                assert expected["responsibility"] in normalized
            for fact in approved_by_case[label]:
                assert fact in normalized, f"{label}: approved fact missing from {format}"
            for sentinel in pending_sentinels:
                assert sentinel not in normalized, f"{label}: pending fact leaked into {format}"
            extracted[format] = text

        assert letter["preview_text"].strip() == extracted["txt"].strip()
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
    assert first["job_id"] != second["job_id"], "cases must target different jobs"
    assert first["application_id"] != second["application_id"]
    assert first["document_id"] != second["document_id"]
    assert first["sha256"]["txt"] != second["sha256"]["txt"], (
        "distinct job contexts produced byte-identical cover letters"
    )
    evidence["base_upload"] = {
        "revision_id": base_revision_id,
        "sha256": base_sha256,
        "unchanged": True,
    }
    return evidence
