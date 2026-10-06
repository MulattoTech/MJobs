"""Explainable skill ordering; never rewrite facts or move work-history lines."""
import hashlib
import json
import re
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skill_mentions

from services.documents.templates import TAILORED_TEMPLATE as TEMPLATE, TAILORED_MODEL as MODEL
from services.documents.errors import PreparationError


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
