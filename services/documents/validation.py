"""Exact assertion validation for deterministic, approved-fact templates."""
from collections import Counter
import re
from docx import Document
from pypdf import PdfReader

from services.documents.errors import PreparationError
from services.documents.templates import (CONTRACTS, REPHRASE_TEMPLATE, LEGACY_REPHRASE_TEMPLATE,
    CONTACT_PAIRS, LEGACY_CONTACT_PAIRS, contact_header, contract_role, resume_layout)
from services.documents.cover_letter import COVER_LETTER_TEMPLATE, build_cover_letter


def _resume_story_context(profile, paragraphs, expected, headings, context, selected_stories):
    from services.career_profile.story_selection import (
        resume_story_paragraphs,
        story_revision_refs,
    )
    refs = story_revision_refs(selected_stories)
    story_paragraphs, story_headings = resume_story_paragraphs(selected_stories)
    if refs:
        offset = len(expected)
        expected.extend(story_paragraphs)
        headings.update(offset + index for index in story_headings)
    if (paragraphs != expected
            or context.get('story_refs', []) != refs
            or context.get('review_required', False) != bool(refs)
            or (refs and (not isinstance(context.get('story_job_id'), str)
                          or not context.get('story_job_id')
                          or not isinstance(context.get('story_job_fingerprint'), str)
                          or not context.get('story_job_fingerprint')))
            or (not refs and (context.get('story_job_id') is not None
                              or context.get('story_job_fingerprint') is not None))):
        raise PreparationError('UNSUPPORTED_CLAIM',
            'Resume differs from its approved facts or reviewed story sources')
    return len(expected) - len(story_paragraphs), headings

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


def validate_assertions(kind, paragraphs, profile, fact_ids, job_title=None, template='verified-facts-v1', generation_context=None, selected_stories=None):
    profile_facts = profile['facts']
    eligible = {row['fact_id']: row['text'] for row in profile_facts
                if row['verified'] and row['user_approved'] and row['confidence'] >= 80 and row['source'].strip()}
    if len(set(fact_ids)) != len(fact_ids) or any(identifier not in eligible for identifier in fact_ids):
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document references unapproved or missing career facts')
    if kind == 'resume':
        if template not in CONTRACTS:
            raise PreparationError('UNSUPPORTED_CLAIM', 'Document template has no assertion validation contract')
        context = generation_context if isinstance(generation_context, dict) else {}
        selected_stories = selected_stories or []
        legacy_rephrase_templates = (LEGACY_REPHRASE_TEMPLATE,
            LEGACY_CONTACT_PAIRS[LEGACY_REPHRASE_TEMPLATE][0])
        current_rewrite_templates = (REPHRASE_TEMPLATE, CONTACT_PAIRS[REPHRASE_TEMPLATE][0])
        if template in (*legacy_rephrase_templates, *current_rewrite_templates):
            context = generation_context if isinstance(generation_context, dict) else {}
            if (context.get('selection_mode') != 'EXPLICIT_APPROVED_FACTS' or
                    context.get('selected_fact_ids') != context.get('original_fact_ids') or
                    set(context.get('selected_fact_ids', [])) != set(fact_ids)):
                raise PreparationError('RESUME_REPHRASE_CONTEXT_INVALID', 'Rewritten resume facts lack an explicit saved selection')
            from services.documents.tailor import apply_saved_user_rephrases, apply_saved_user_rewrites
            by_id = {row['fact_id']:row for row in profile_facts}
            rows = [dict(by_id[identifier]) for identifier in fact_ids]
            if template in current_rewrite_templates:
                rewritten = apply_saved_user_rewrites(rows, fact_ids, context.get('fact_rephrases'))
            else:
                rewritten = apply_saved_user_rephrases(rows, fact_ids, context.get('fact_rephrases'))
            expected, heading_indices = resume_layout(profile, rewritten, template)
            base_length, heading_indices = _resume_story_context(
                profile, paragraphs, expected, heading_indices, context, selected_stories)
            header = len(contact_header(profile, template))
            headings = {expected[index] for index in heading_indices}
            actual_facts = [value for value in paragraphs[header:base_length] if value not in headings]
            _reject_novel_tokens(actual_facts, [eligible[identifier] for identifier in fact_ids])
            if paragraphs != expected:
                raise PreparationError('UNSUPPORTED_CLAIM', 'Rewritten resume differs from its reviewed source-fact context')
            claims = _claim_evidence(fact_ids, eligible, profile_facts)
            changed = {row['fact_id'] for row in context['fact_rephrases']}
            for claim in claims['claims']:
                if claim['fact_id'] in changed:
                    claim['decision'] = 'USER_ATTESTED_FACTUAL_PARAPHRASE'
                    claim['protected_fields'].append('USER_REVIEWED_WORDING_BOUND_TO_SOURCE_SHA256')
            claims['strategy'] = 'USER_REVIEWED_FACTUAL_PARAPHRASE_WITH_EXACT_NUMERIC_PRESERVATION'
            return claims
        if CONTRACTS[template][2]:
            by_id = {row['fact_id']:row for row in profile['facts']}
            expected, heading_indices = resume_layout(profile, [by_id[i] for i in fact_ids], template)
            base_length, heading_indices = _resume_story_context(
                profile, paragraphs, expected, heading_indices, context, selected_stories)
            header = len(contact_header(profile, template))
            headings = {expected[index] for index in heading_indices}
            actual_facts = [value for value in paragraphs[header:base_length] if value not in headings]
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
                context.get('style'), context.get('length'),
                selected_stories=selected_stories)
        except PreparationError as error:
            raise PreparationError('UNSUPPORTED_CLAIM', 'Cover letter evidence is no longer valid') from error
        fact_paragraphs = [value for value in paragraphs
                           if value.startswith('Approved career facts relevant to these details:')]
        _reject_novel_tokens(fact_paragraphs,
                             [eligible[identifier] for identifier in expected_context['fact_ids']])
        if (paragraphs != expected or fact_ids != expected_context['fact_ids']
                or context.get('fact_ids') != expected_context['fact_ids']
                or context.get('story_refs', []) != expected_context.get('story_refs', [])
                or context.get('review_required', False) != expected_context.get('review_required', False)):
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


def validate_stored_resume(paths, profile, fact_ids, template='verified-facts-v1', model=None,
                           generation_context=None, selected_stories=None):
    if not contract_role(template, model, allow_legacy_unrecorded=True):
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document template and generation model have no validation contract')
    if 'docx' not in paths:
        raise PreparationError('DOCUMENT_CONTENT_MISMATCH', 'DOCX is required for assertion validation')
    paragraphs = _paragraphs(paths['docx'])
    validate_assertions('resume', paragraphs, profile, fact_ids, template=template,
                        generation_context=generation_context,
                        selected_stories=selected_stories)
    validate_formats(paths, paragraphs)
