"""Exact assertion validation for deterministic, approved-fact templates."""
from collections import Counter
from docx import Document
from pypdf import PdfReader

from services.documents.errors import PreparationError

VALIDATOR = 'exact-approved-facts-v1'


def _read(path, reader):
    try:
        return reader(path)
    except Exception as error:
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'Stored document cannot be read for validation') from error


def _paragraphs(path):
    return _read(path, lambda p: [paragraph.text for paragraph in Document(p).paragraphs])


def validate_assertions(kind, paragraphs, profile, fact_ids, job_title=None):
    eligible = {row['fact_id']: row['text'] for row in profile['facts']
                if row['verified'] and row['user_approved'] and row['confidence'] >= 80 and row['source'].strip()}
    if len(set(fact_ids)) != len(fact_ids) or any(identifier not in eligible for identifier in fact_ids):
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document references unapproved or missing career facts')
    if kind == 'resume':
        header = [profile['name'], profile['email']]
    elif kind == 'cover-letter' and job_title is not None:
        header = [profile['name'], f'Application for {job_title}']
    else:
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document template has no assertion validation contract')
    if paragraphs[:2] != header or Counter(paragraphs[2:]) != Counter(eligible[i] for i in fact_ids):
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document assertions differ from the approved factual inputs')


def validate_formats(paths, paragraphs):
    if not {'docx', 'pdf'}.issubset(paths):
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'Required document formats are missing')
    actual = _paragraphs(paths['docx'])
    if actual != paragraphs:
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'DOCX content differs from validated assertions')
    if 'txt' in paths and _read(paths['txt'], lambda p:p.read_text(encoding='utf-8')) != '\n'.join(paragraphs)+'\n':
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'Plain text differs from validated assertions')
    pdf_text = _read(paths['pdf'], lambda p:' '.join(page.extract_text() for page in PdfReader(p).pages))
    # PDF layout adds line wraps and spaces; every non-whitespace token must
    # still match in order, including case, punctuation and Unicode codepoints.
    if pdf_text.split() != ' '.join(paragraphs).split():
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'PDF content differs from validated assertions')


def validate_stored_resume(paths, profile, fact_ids):
    if 'docx' not in paths:
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'DOCX is required for assertion validation')
    paragraphs = _paragraphs(paths['docx'])
    validate_assertions('resume', paragraphs, profile, fact_ids)
    validate_formats(paths, paragraphs)
