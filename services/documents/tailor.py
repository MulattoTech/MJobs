"""Explainable skill ordering; never rewrite facts or move work-history lines."""
import hashlib
import json
import re
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skill_mentions

from services.documents.templates import TAILORED_TEMPLATE as TEMPLATE, TAILORED_MODEL as MODEL


def target_fingerprint(job):
    return hashlib.sha256(json.dumps({key:job[key] for key in ('id','title','skills')},
        ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def order_skills(facts, job):
    targets=[]
    seen=set()
    for label in job['skills']:
        label=label.strip()
        canonical=canonical_skill_id(label)
        key=canonical or label.casefold()
        if label and key not in seen:
            seen.add(key);targets.append((label,canonical))
    skills=[row for row in facts if row['kind']=='skill']
    matches={}
    for row in skills:
        mentions={mention['id'] for mention in extract_skill_mentions(row['text'])}
        exact=canonical_skill_id(row['text'])
        if exact:mentions.add(exact)
        matches[row['fact_id']]=[label for label,canonical in targets if
            (canonical in mentions if canonical else bool(re.search(
                r'(?<![\w+#.])'+re.escape(label)+r'(?![\w+#.])',row['text'],re.I)))]
    # Python's stable sort retains original order for equally relevant skills.
    ranked=iter(sorted(skills,key=lambda row:-len(matches[row['fact_id']])))
    ordered=[next(ranked) if row['kind']=='skill' else row for row in facts]
    return ordered, {'mode':'APPROVED_SKILL_ORDER','job_id':job['id'],'job_title':job['title'],
        'job_skills':list(job['skills']),'target_fingerprint':target_fingerprint(job),
        'original_fact_ids':[row['fact_id'] for row in facts],
        'ordered_fact_ids':[row['fact_id'] for row in ordered],'matched_skills':matches}
