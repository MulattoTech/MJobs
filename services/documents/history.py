"""Read document evidence from its original immutable profile snapshot."""
import hashlib
from pathlib import Path

from sqlalchemy import select
from packages.persistence import models as m
from services.documents.avoid_list import validate_content as validate_avoid_list
from services.documents.errors import PreparationError
from services.documents.validation import validate_assertions, validate_formats


def validated_letter_preview(store, document, output_dir):
    application = store.application(document["application_id"])
    document.setdefault("profile_id", application["profile_id"])
    root = Path(output_dir).resolve()
    application_root = (root / document["application_id"]).resolve()
    if not application_root.is_relative_to(root):
        raise PreparationError("DOCUMENT_CONTENT_MISMATCH", "Cover-letter storage path is invalid")
    paths = {}
    for format, relative in document["files"].items():
        path = (application_root / relative).resolve()
        if format not in ("docx", "pdf", "txt") or not path.is_relative_to(application_root) or not path.is_file():
            raise PreparationError("DOCUMENT_CONTENT_MISMATCH", "Cover-letter file is missing or outside its revision")
        if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"].get(format):
            raise PreparationError("DOCUMENT_CONTENT_MISMATCH", "Cover-letter integrity check failed")
        paths[format] = path
    if not {"docx", "pdf", "txt"}.issubset(paths):
        raise PreparationError("DOCUMENT_CONTENT_MISMATCH", "Validated cover-letter formats are incomplete")
    paragraphs = paths["txt"].read_text(encoding="utf-8").splitlines()
    profile = store.profile(document["profile_id"], document["profile_version"])
    validate_assertions("cover-letter", paragraphs, profile, document["fact_ids"],
                        document.get("job_title"), document["template"],
                        generation_context=document.get("generation_context"))
    validate_formats(paths, paragraphs)
    current_profile = store.profile(document["profile_id"])
    validate_avoid_list('\n'.join(paragraphs), current_profile.get('preferences'), 'cover letter')
    return "\n".join(paragraphs)


def application_documents(store, application_id, output_dir=None):
    application = store.application(application_id)
    current_preferences = store.profile(application['profile_id']).get('preferences', {})
    with store.engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(select(m.documents).where(
            m.documents.c.application_id == application_id).order_by(
            m.documents.c.version.desc(), m.documents.c.kind)).mappings()]
        facts = {}
        versions = {row['profile_version'] for row in rows}
        if versions:
            for fact in connection.execute(select(m.facts).where(
                m.facts.c.profile_id == application['profile_id'], m.facts.c.version.in_(versions)
            )).mappings():
                facts[(fact['version'], fact['fact_id'])] = dict(fact)
        for row in rows:
            row['job_id'] = application['job_id']
            row['profile_id'] = application['profile_id']
            row['legacy_job_context_missing'] = bool(row['kind'] == 'cover-letter'
                and row['template'] == 'verified-facts-v1' and not row.get('generation_context'))
            row['facts_used'] = [facts[(row['profile_version'], identifier)] for identifier in row['fact_ids']
                                 if (row['profile_version'], identifier) in facts]
            row['missing_fact_ids'] = [identifier for identifier in row['fact_ids']
                                       if (row['profile_version'], identifier) not in facts]
            row['avoid_list_error'] = None
            if output_dir is not None:
                try:
                    relative = row['files'].get('txt')
                    root = Path(output_dir).resolve()
                    application_root = (root / application_id).resolve()
                    path = (application_root / relative).resolve() if isinstance(relative, str) else root
                    if not application_root.is_relative_to(root) or not path.is_relative_to(application_root):
                        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'Document storage path is invalid')
                    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'].get('txt'):
                        text = path.read_text(encoding='utf-8')
                        validate_avoid_list(text, current_preferences,
                                            'application answer' if row['kind'] == 'application-answer-export'
                                            else 'resume' if row['kind'] == 'resume' else 'cover letter')
                except PreparationError as error:
                    if error.code == 'AVOID_LIST_BLOCKED':
                        row['avoid_list_error'] = str(error)
            if row['kind'] == 'cover-letter' and output_dir is not None:
                try:
                    row['preview_text'] = validated_letter_preview(store, row, output_dir)
                    row['preview_error'] = None
                except (OSError, ValueError, KeyError, TypeError):
                    row['preview_text'] = None
                    row['preview_error'] = "Cover-letter validation failed. Revise and prepare a new version before using this file."
        return rows
