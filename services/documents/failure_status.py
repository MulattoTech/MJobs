"""Fail-closed status and output checks for generated application documents."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
import re

from docx import Document
from pypdf import PdfReader

from services.documents.errors import PreparationError
from services.documents.validation import validate_formats


DOCUMENT_GENERATION_FAILED = "DOCUMENT_GENERATION_FAILED"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_FORMATS = ("docx", "pdf", "txt")


def _failed():
    raise PreparationError(
        DOCUMENT_GENERATION_FAILED,
        "Document generation failed validation. Generate a new revision before using this file.",
    )


def validate_generated_outputs(
    files: Mapping[str, str | Path],
    expected_sha256: Mapping[str, str] | None = None,
    required_formats: tuple[str, ...] = _DEFAULT_FORMATS,
) -> dict:
    """Validate all generated formats before a document revision can be ready.

    This checks required files, nonempty readable formats, cross-format text
    consistency, and optional persisted content hashes. It returns a ready
    record only after every check succeeds. All invalid output cases share the
    stable ``DOCUMENT_GENERATION_FAILED`` error code.
    """
    if (not isinstance(files, Mapping)
            or not isinstance(required_formats, (tuple, list))
            or not required_formats
            or any(not isinstance(format, str) for format in required_formats)
            or len(set(required_formats)) != len(required_formats)
            or any(format not in _DEFAULT_FORMATS for format in required_formats)
            or any(format not in files for format in required_formats)):
        _failed()

    paths: dict[str, Path] = {}
    actual_hashes: dict[str, str] = {}
    for format in required_formats:
        value = files[format]
        if not isinstance(value, (str, Path)):
            _failed()
        path = Path(value)
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                _failed()
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if expected_sha256 is not None:
                expected = expected_sha256.get(format) if isinstance(expected_sha256, Mapping) else None
                if not isinstance(expected, str) or not _SHA256.fullmatch(expected) or digest != expected:
                    _failed()
            if format == "txt":
                text = content.decode("utf-8")
                if not text.strip():
                    _failed()
            elif format == "docx":
                paragraphs = [paragraph.text for paragraph in Document(path).paragraphs]
                if not any(paragraph.strip() for paragraph in paragraphs):
                    _failed()
            elif format == "pdf":
                reader = PdfReader(path, strict=True)
                if not reader.pages or not "".join(page.extract_text() or "" for page in reader.pages).strip():
                    _failed()
            paths[format] = path
            actual_hashes[format] = digest
        except PreparationError:
            raise
        except Exception as error:
            raise PreparationError(
                DOCUMENT_GENERATION_FAILED,
                "Document generation failed validation. Generate a new revision before using this file.",
            ) from error

    if set(required_formats) == set(_DEFAULT_FORMATS):
        try:
            paragraphs = [paragraph.text for paragraph in Document(paths["docx"]).paragraphs]
            validate_formats(paths, paragraphs)
        except Exception as error:
            raise PreparationError(
                DOCUMENT_GENERATION_FAILED,
                "Document generation failed validation. Generate a new revision before using this file.",
            ) from error

    return {
        "status": "READY",
        "ready": True,
        "formats": sorted(required_formats),
        "sha256": actual_hashes,
    }


def failure_status(error: BaseException | None = None) -> dict[str, str]:
    """Return the sanitized terminal classification and safe recovery action."""
    # The input is deliberately not copied into this record: exceptions can
    # contain private profile data, local paths, or provider response content.
    del error
    return {
        "status": DOCUMENT_GENERATION_FAILED,
        "error_code": DOCUMENT_GENERATION_FAILED,
        "recovery": "REGENERATE",
    }
