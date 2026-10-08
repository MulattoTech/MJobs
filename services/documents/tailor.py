"""Explainable skill ordering and user-attested wording for selected approved facts."""
import hashlib
import json
import re
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skill_mentions

from services.documents.templates import TAILORED_TEMPLATE as TEMPLATE, TAILORED_MODEL as MODEL
from services.documents.errors import PreparationError

REWRITABLE_RESUME_FACT_KINDS = {
    'summary', 'responsibility', 'experience', 'accomplishment', 'project', 'technology_experience'
}


def _validate_user_rewrites(facts, selected_fact_ids, rewrites, *, allowed_kinds, include_kind):
    if rewrites is None:
        return [dict(row) for row in facts], []
    if (type(rewrites) is not list or not 1 <= len(rewrites) <= 100 or
            type(selected_fact_ids) is not list or not selected_fact_ids or
            len(set(selected_fact_ids)) != len(selected_fact_ids)):
        raise PreparationError('RESUME_REPHRASE_INVALID', 'Select approved resume facts before rewriting')
    eligible = {row['fact_id']: row for row in eligible_rows({'facts': facts})}
    selected = set(selected_fact_ids)
    provenance = []
    replacement_text = {}
    from services.documents.validation import _DATE_TOKEN, _NUMBER_TOKEN
    for item in rewrites:
        if (type(item) is not dict or set(item) != {'fact_id', 'text', 'reviewed'} or
                type(item['fact_id']) is not str or type(item['text']) is not str or
                type(item['reviewed']) is not bool):
            raise PreparationError('RESUME_REPHRASE_INVALID', 'Resume bullet wording has an invalid review record')
        fact_id = item['fact_id']
        source = eligible.get(fact_id)
        if source is None or fact_id not in selected or source['kind'] not in allowed_kinds:
            raise PreparationError('RESUME_REPHRASE_FACT_INVALID',
                'Only selected approved resume facts in supported sections may be rewritten')
        text = item['text'].strip()
        if not item['reviewed']:
            raise PreparationError('RESUME_REPHRASE_REVIEW_REQUIRED', 'Review each rewritten resume fact before preparing documents')
        if not text or len(text) > 10_000 or '\x00' in text or text == source['text']:
            raise PreparationError('RESUME_REPHRASE_INVALID', 'Enter changed, nonempty resume wording within the supported limit')
        if (sorted(value.casefold() for value in _DATE_TOKEN.findall(text)) !=
                sorted(value.casefold() for value in _DATE_TOKEN.findall(source['text'])) or
                sorted(value.casefold() for value in _NUMBER_TOKEN.findall(text)) !=
                sorted(value.casefold() for value in _NUMBER_TOKEN.findall(source['text']))):
            raise PreparationError('RESUME_REPHRASE_FACTS_CHANGED', 'Rewritten wording must preserve every source date and number exactly')
        replacement_text[fact_id] = text
        record = {
            'fact_id': fact_id,
            'source_text_sha256': hashlib.sha256(source['text'].encode('utf-8')).hexdigest(),
            'text': text,
            'reviewed': True,
            'review_policy': 'USER_CONFIRMED_FACTUAL_PARAPHRASE',
        }
        if include_kind:
            record['fact_kind'] = source['kind']
        provenance.append(record)
    if len(replacement_text) != len(rewrites):
        raise PreparationError('RESUME_REPHRASE_INVALID', 'Each resume fact may be rewritten only once')
    return [dict(row, text=replacement_text.get(row['fact_id'], row['text'])) for row in facts], provenance


def validate_user_rephrases(facts, selected_fact_ids, rewrites):
    """Preserve the original v1 contract: reviewed responsibility bullets only."""
    return _validate_user_rewrites(facts, selected_fact_ids, rewrites,
                                   allowed_kinds={'responsibility'}, include_kind=False)


def validate_user_rewrites(facts, selected_fact_ids, rewrites):
    """Apply reviewed wording to selected approved summary and resume-bullet facts."""
    return _validate_user_rewrites(facts, selected_fact_ids, rewrites,
                                   allowed_kinds=REWRITABLE_RESUME_FACT_KINDS, include_kind=True)


def apply_saved_user_rephrases(facts, selected_fact_ids, saved):
    """Rebuild a historical user-reviewed rewrite from its immutable source facts."""
    if type(saved) is not list or not saved:
        raise PreparationError('RESUME_REPHRASE_CONTEXT_INVALID', 'Saved resume wording review is unavailable')
    requests=[]
    for item in saved:
        if (type(item) is not dict or set(item) != {
                'fact_id','source_text_sha256','text','reviewed','review_policy'} or
                item.get('reviewed') is not True or
                item.get('review_policy') != 'USER_CONFIRMED_FACTUAL_PARAPHRASE' or
                type(item.get('source_text_sha256')) is not str):
            raise PreparationError('RESUME_REPHRASE_CONTEXT_INVALID', 'Saved resume wording review is invalid')
        requests.append({'fact_id':item['fact_id'],'text':item['text'],'reviewed':True})
    rewritten, expected = validate_user_rephrases(facts, selected_fact_ids, requests)
    if any(actual['source_text_sha256'] != item['source_text_sha256']
           for actual,item in zip(expected,saved)):
        raise PreparationError('RESUME_REPHRASE_SOURCE_CHANGED', 'Saved resume wording is not bound to its original fact')
    return rewritten


def apply_saved_user_rewrites(facts, selected_fact_ids, saved):
    """Rebuild v2 historical summary/responsibility rewrites from their source facts."""
    if type(saved) is not list or not saved:
        raise PreparationError('RESUME_REPHRASE_CONTEXT_INVALID', 'Saved resume wording review is unavailable')
    requests=[]
    for item in saved:
        if (type(item) is not dict or set(item) != {
                'fact_id','fact_kind','source_text_sha256','text','reviewed','review_policy'} or
                item.get('reviewed') is not True or
                item.get('review_policy') != 'USER_CONFIRMED_FACTUAL_PARAPHRASE' or
                type(item.get('source_text_sha256')) is not str or
                item.get('fact_kind') not in REWRITABLE_RESUME_FACT_KINDS):
            raise PreparationError('RESUME_REPHRASE_CONTEXT_INVALID', 'Saved resume wording review is invalid')
        requests.append({'fact_id':item['fact_id'],'text':item['text'],'reviewed':True})
    rewritten, expected = validate_user_rewrites(facts, selected_fact_ids, requests)
    if any(actual['source_text_sha256'] != item['source_text_sha256'] or
           actual['fact_kind'] != item['fact_kind'] for actual,item in zip(expected,saved)):
        raise PreparationError('RESUME_REPHRASE_SOURCE_CHANGED', 'Saved resume wording is not bound to its original fact')
    return rewritten


def eligible_rows(profile):
    return [row for row in profile['facts'] if row['verified'] and row['user_approved'] and
            row['confidence'] >= 80 and row['source'].strip()]


def select_resume_rows(eligible, tailor_resume, requested):
    if requested is None:
        return eligible
    if (not tailor_resume or type(requested) is not list or not 1 <= len(requested) <= 1000 or
        any(type(value) is not str or not value.strip() or len(value)>128 for value in requested) or
        len(set(requested)) != len(requested)):
        raise PreparationError('DOCUMENT_SELECTION_INVALID', 'Choose a nonempty unique set of approved resume facts with tailoring enabled')
    chosen=set(requested)
    if not chosen.issubset({row['fact_id'] for row in eligible}):
        raise PreparationError('DOCUMENT_SELECTION_INVALID', 'A selected resume fact is missing or no longer approved for use')
    return [row for row in eligible if row['fact_id'] in chosen]


def selection_fingerprint(identifiers):
    return hashlib.sha256(json.dumps(identifiers,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()


def target_fingerprint(job):
    return hashlib.sha256(json.dumps({key:job[key] for key in ('id','title','skills')},
        ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def matched_labels(facts, job):
    targets=[]
    seen=set()
    for label in job['skills']:
        label=label.strip()
        canonical=canonical_skill_id(label)
        key=canonical or label.casefold()
        if label and key not in seen:
            seen.add(key);targets.append((label,canonical))
    matches={}
    for row in facts:
        mentions={mention['id'] for mention in extract_skill_mentions(row['text'])}
        exact=canonical_skill_id(row['text'])
        if exact:mentions.add(exact)
        matches[row['fact_id']]=[label for label,canonical in targets if
            (canonical in mentions if canonical else bool(re.search(
                r'(?<![\w+#.])'+re.escape(label)+r'(?![\w+#.])',row['text'],re.I)))]
    return matches


def emphasis(facts, job, include_responsibilities=False):
    kinds=('skill','technology_experience','responsibility') if include_responsibilities else ('skill','technology_experience')
    matches=matched_labels([row for row in facts if row['kind'] in kinds],job)
    return [row['fact_id'] for row in facts if matches.get(row['fact_id'])]


def select_summary_rows(facts, job):
    """Keep exact approved summary facts that mention target skills when any match."""
    summaries=[row for row in facts if row['kind']=='summary']
    matches=matched_labels(summaries,job)
    selected_ids=[row['fact_id'] for row in summaries if matches[row['fact_id']]]
    adjusted=bool(selected_ids)
    selected=set(selected_ids) if adjusted else {row['fact_id'] for row in summaries}
    result=[row for row in facts if row['kind']!='summary' or row['fact_id'] in selected]
    return result,{
        'mode':'EXACT_TARGET_SKILL_SUMMARY_FACTS',
        'adjusted':adjusted,
        'original_summary_fact_ids':[row['fact_id'] for row in summaries],
        'selected_summary_fact_ids':[row['fact_id'] for row in summaries if row['fact_id'] in selected],
        'omitted_summary_fact_ids':[row['fact_id'] for row in summaries if row['fact_id'] not in selected],
        'matched_skills':matches,
    }


def order_skills(facts, job, include_responsibilities=False):
    skills=[row for row in facts if row['kind']=='skill']
    kinds=('skill','technology_experience','responsibility') if include_responsibilities else ('skill','technology_experience')
    matches=matched_labels([row for row in facts if row['kind'] in kinds],job)
    # Python's stable sort retains original order for equally relevant skills.
    ranked=iter(sorted(skills,key=lambda row:-len(matches[row['fact_id']])))
    ordered=[next(ranked) if row['kind']=='skill' else row for row in facts]
    return ordered, {'mode':'APPROVED_SKILL_ORDER','job_id':job['id'],'job_title':job['title'],
        'job_skills':list(job['skills']),'target_fingerprint':target_fingerprint(job),
        'original_fact_ids':[row['fact_id'] for row in facts],
        'ordered_fact_ids':[row['fact_id'] for row in ordered],'matched_skills':matches}
