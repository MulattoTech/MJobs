"""Exact assertion validation for deterministic, approved-fact templates."""
from collections import Counter
import re
from docx import Document
from pypdf import PdfReader

from services.documents.errors import PreparationError
from services.documents.templates import CONTRACTS, contact_header, contract_role, resume_layout
from services.documents.cover_letter import COVER_LETTER_TEMPLATE, build_cover_letter

VALIDATOR = 'exact-approved-facts-v5-claim-preservation'
_DATE_TOKEN = re.compile(
    r"\b(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+\d{1,2},?\s+(?:19|20)\d{2}|(?:19|20)\d{2}"
    r"(?:\s*[-–—/]\s*(?:(?:19|20)?\d{2}))?)\b", re.IGNORECASE)
_NUMBER_TOKEN = re.compile(
    r"(?<![\w])(?:[$€£]\s*)?[+-]?\d+(?:[,.]\d+)*"
    r"(?:\s?%|\s*(?:percent|percentages|x|times|hours?|days?|years?|months?|"
    r"users?|employees?))?(?![\w])", re.IGNORECASE)


def _read(path, reader):
    try:
        return reader(path)
    except Exception as error:
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'Stored document cannot be read for validation') from error


def _paragraphs(path):
    return _read(path, lambda p: [paragraph.text for paragraph in Document(p).paragraphs])


def pdf_tokens_match(actual, expected):
    """Permit physical wraps within an exact URL, never within factual words."""
    tokens=actual.split();cursor=0
    for wanted in expected.split():
        if cursor>=len(tokens):return False
        observed=tokens[cursor];cursor+=1
        if wanted.casefold().startswith(('http://','https://')):
            while observed!=wanted and wanted.startswith(observed) and cursor<len(tokens):
                observed+=tokens[cursor];cursor+=1
        if observed!=wanted:return False
    return cursor==len(tokens)


def _claim_evidence(fact_ids, eligible, profile_facts):
    by_id = {row['fact_id']: row for row in profile_facts}
    claims = []
    for fact_id in fact_ids:
        text = eligible[fact_id]
        row = by_id[fact_id]
        dates = [value.casefold() for value in _DATE_TOKEN.findall(text)]
        numbers = [value.casefold() for value in _NUMBER_TOKEN.findall(text)]
        kind = row.get('kind', 'skill')
        fields = ['FACT_TEXT', 'DATE_TOKENS', 'NUMERIC_TOKENS']
        if kind == 'experience':
            fields.append('EMPLOYER_TEXT_WITHIN_EXPERIENCE_FACT')
        elif kind == 'education':
            fields.extend(('DEGREE_TEXT_WITHIN_EDUCATION_FACT',
                           'INSTITUTION_TEXT_WITHIN_EDUCATION_FACT'))
        elif kind == 'certification':
            fields.append('CREDENTIAL_TEXT_WITHIN_CERTIFICATION_FACT')
        claims.append({
            'fact_id': fact_id,
            'kind': kind,
            'decision': 'PRESERVED_EXACT',
            'protected_fields': fields,
            'protected_token_counts': {'dates': len(dates), 'numbers': len(numbers)},
        })
    return {
        'strategy': 'DETERMINISTIC_EXACT_FACT_PRESERVATION',
        'keep_drop_authority': 'TEMPLATE_AND_USER_SELECTION',
        'claims': claims,
    }


def _reject_novel_tokens(actual_texts, source_texts):
    for pattern, claim_name in ((_DATE_TOKEN, 'date'), (_NUMBER_TOKEN, 'numeric')):
        approved = Counter(value.casefold() for text in source_texts for value in pattern.findall(text))
        generated = Counter(value.casefold() for text in actual_texts for value in pattern.findall(text))
        if generated - approved:
            raise PreparationError('UNSUPPORTED_CLAIM',
                                   f'Document adds a {claim_name} claim absent from approved facts')


def validate_assertions(kind, paragraphs, profile, fact_ids, job_title=None, template='verified-facts-v1', generation_context=None):
    profile_facts = profile['facts']
    eligible = {row['fact_id']: row['text'] for row in profile_facts
                if row['verified'] and row['user_approved'] and row['confidence'] >= 80 and row['source'].strip()}
    if len(set(fact_ids)) != len(fact_ids) or any(identifier not in eligible for identifier in fact_ids):
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document references unapproved or missing career facts')
    if kind == 'resume':
        if template not in CONTRACTS:
            raise PreparationError('UNSUPPORTED_CLAIM', 'Document template has no assertion validation contract')
        if CONTRACTS[template][2]:
            by_id = {row['fact_id']:row for row in profile['facts']}
            expected, heading_indices = resume_layout(profile, [by_id[i] for i in fact_ids], template)
            header = len(contact_header(profile, template))
            headings = {expected[index] for index in heading_indices}
            actual_facts = [value for value in paragraphs[header:] if value not in headings]
            _reject_novel_tokens(actual_facts, [eligible[identifier] for identifier in fact_ids])
            if paragraphs != expected:
                raise PreparationError('UNSUPPORTED_CLAIM', 'Document assertions differ from the approved factual inputs and template structure')
            return _claim_evidence(fact_ids, eligible, profile_facts)
        header = [profile['name'], profile['email']]
    elif kind == 'cover-letter' and template == COVER_LETTER_TEMPLATE:
        context = generation_context if isinstance(generation_context, dict) else {}
        snapshot = context.get('job_snapshot')
        if context.get('mode') != 'JOB_SPECIFIC_APPROVED_FACTS' or not isinstance(snapshot, dict):
            raise PreparationError('UNSUPPORTED_CLAIM', 'Cover letter has no validated job-evidence snapshot')
        try:
            expected, expected_context = build_cover_letter(profile, snapshot, profile['facts'],
                context.get('style'), context.get('length'))
        except PreparationError as error:
            raise PreparationError('UNSUPPORTED_CLAIM', 'Cover letter evidence is no longer valid') from error
        _reject_novel_tokens([paragraphs[5]] if len(paragraphs) > 5 else [],
                             [eligible[identifier] for identifier in expected_context['fact_ids']])
        if (paragraphs != expected or fact_ids != expected_context['fact_ids']
                or context.get('fact_ids') != expected_context['fact_ids']):
            raise PreparationError('UNSUPPORTED_CLAIM', 'Cover letter differs from the approved facts or saved job evidence')
        return _claim_evidence(fact_ids, eligible, profile_facts)
    elif kind == 'cover-letter' and template == 'verified-facts-v1':
        prefix = 'Application for '
        if (len(paragraphs) < 2 or paragraphs[0] != profile['name']
                or not paragraphs[1].startswith(prefix) or not paragraphs[1][len(prefix):].strip()):
            raise PreparationError('UNSUPPORTED_CLAIM', 'Legacy cover-letter identity header is invalid')
        # Legacy rows have no immutable job snapshot. Validate the title embedded
        # in the hash-checked artifact, not today's mutable canonical job title.
        header = paragraphs[:2]
    elif kind == 'cover-letter' and job_title is not None:
        header = [profile['name'], f'Application for {job_title}']
    else:
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document template has no assertion validation contract')
    _reject_novel_tokens(paragraphs[2:], [eligible[identifier] for identifier in fact_ids])
    if paragraphs[:2] != header or Counter(paragraphs[2:]) != Counter(eligible[i] for i in fact_ids):
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document assertions differ from the approved factual inputs')
    return _claim_evidence(fact_ids, eligible, profile_facts)


def validate_formats(paths, paragraphs):
    if not {'docx', 'pdf'}.issubset(paths):
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'Required document formats are missing')
    actual = _paragraphs(paths['docx'])
    if actual != paragraphs:
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'DOCX content differs from validated assertions')
    if 'txt' in paths and _read(paths['txt'], lambda p:p.read_text(encoding='utf-8')) != '\n'.join(paragraphs)+'\n':
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'Plain text differs from validated assertions')
    pdf_text = _read(paths['pdf'], lambda p:' '.join(page.extract_text() for page in PdfReader(p).pages))
    # Long URLs can wrap mid-token. Their characters must still match exactly;
    # ordinary factual tokens retain strict word boundaries and ordering.
    if not pdf_tokens_match(pdf_text,' '.join(paragraphs)):
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'PDF content differs from validated assertions')


def validate_stored_resume(paths, profile, fact_ids, template='verified-facts-v1', model=None):
    if not contract_role(template, model, allow_legacy_unrecorded=True):
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document template and generation model have no validation contract')
    if 'docx' not in paths:
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'DOCX is required for assertion validation')
    paragraphs = _paragraphs(paths['docx'])
    validate_assertions('resume', paragraphs, profile, fact_ids, template=template)
    validate_formats(paths, paragraphs)
