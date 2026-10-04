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


def emphasis(facts, job):
    matches=matched_labels([row for row in facts if row['kind'] in ('skill','technology_experience')],job)
    return [row['fact_id'] for row in facts if matches.get(row['fact_id'])]


def order_skills(facts, job):
    skills=[row for row in facts if row['kind']=='skill']
    matches=matched_labels([row for row in facts if row['kind'] in ('skill','technology_experience')],job)
    # Python's stable sort retains original order for equally relevant skills.
    ranked=iter(sorted(skills,key=lambda row:-len(matches[row['fact_id']])))
    ordered=[next(ranked) if row['kind']=='skill' else row for row in facts]
    return ordered, {'mode':'APPROVED_SKILL_ORDER','job_id':job['id'],'job_title':job['title'],
        'job_skills':list(job['skills']),'target_fingerprint':target_fingerprint(job),
        'original_fact_ids':[row['fact_id'] for row in facts],
        'ordered_fact_ids':[row['fact_id'] for row in ordered],'matched_skills':matches}
