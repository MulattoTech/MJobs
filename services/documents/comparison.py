"""Read-only comparisons of explicitly selected, immutable generated resumes."""
import hashlib
from pathlib import Path
from sqlalchemy.exc import NoResultFound

from services.documents import tailor
from services.documents.history import application_documents
from services.documents.validation import validate_assertions
from services.documents.errors import PreparationError


class ComparisonUnavailable(ValueError):
    pass


def compare_facts(base, target, matches):
    old={row['fact_id']:row for row in base};new={row['fact_id']:row for row in target}
    summary=[];bullets=[]
    for identifier in dict.fromkeys([*old,*new]):
        before=old.get(identifier);after=new.get(identifier)
        if before and after and (before['text'],before['kind'])==(after['text'],after['kind']):
            continue
        change={'fact_id':identifier,'before':before,'after':after}
        if (before and before['kind']=='summary') or (after and after['kind']=='summary'):
            summary.append(change)
        elif before and after:
            bullets.append(change)
    old_common=[identifier for identifier in old if identifier in new]
    new_common=[identifier for identifier in new if identifier in old]
    return {'summary_changes':summary,'bullet_changes':bullets,
        'reordered':[{'fact_id':identifier,'text':new[identifier]['text'],
                      'before_position':list(old).index(identifier)+1,'after_position':list(new).index(identifier)+1}
                     for identifier in new_common if old_common.index(identifier)!=new_common.index(identifier)],
        'emphasized_skills':[dict(row,job_skills=list(matches[row['fact_id']])) for row in target
                             if row['kind']=='skill' and matches.get(row['fact_id'])],
        'omitted':[row for row in base if row['fact_id'] not in new],
        'added':[row for row in target if row['fact_id'] not in old]}


def _snapshot(store, output_dir, row):
    if row['missing_fact_ids'] or len(row['facts_used'])!=len(row['fact_ids']):
        raise ComparisonUnavailable('Original fact history is unavailable for this revision')
    root=Path(output_dir).resolve();app_root=(root/row['application_id']).resolve()
    relative=row['files'].get('txt')
    path=(app_root/relative).resolve() if isinstance(relative,str) else root
    if not app_root.is_relative_to(root) or not path.is_relative_to(app_root) or not path.is_file():
        raise ComparisonUnavailable('Original plain-text document is unavailable')
    try:
        if path.stat().st_size>10*1024*1024:
            raise ComparisonUnavailable('Original document exceeds the comparison size limit')
        content=path.read_bytes()
        if hashlib.sha256(content).hexdigest()!=row['sha256'].get('txt'):
            raise ComparisonUnavailable('Original document integrity check failed')
        text=content.decode('utf-8')
        profile=store.profile(row['profile_id'],row['profile_version'])
        paragraphs=[profile['name'],profile['email'],*[fact['text'] for fact in row['facts_used']]]
        validate_assertions('resume',paragraphs,profile,row['fact_ids'])
        if text!='\n'.join(paragraphs)+'\n':
            raise ComparisonUnavailable('Stored text differs from its original fact history')
    except (OSError,UnicodeError,PreparationError,NoResultFound) as error:
        raise ComparisonUnavailable('Original document or approved fact history is unavailable') from error
    facts=[{key:fact[key] for key in ('fact_id','kind','text')} for fact in row['facts_used']]
    return {'id':row['id'],'version':row['version'],'profile_version':row['profile_version'],
            'text':text,'sha256':row['sha256']['txt'],'facts':facts,'contact':paragraphs[:2]}


def compare_resumes(store, output_dir, application_id, base_document_id, tailored_document_id):
    rows={row['id']:row for row in application_documents(store,application_id)}
    base=rows.get(base_document_id);target=rows.get(tailored_document_id)
    if (not base or not target or base['kind']!='resume' or target['kind']!='resume' or
        base['template']!='verified-facts-v1' or base['generation_model']!='deterministic-fact-copy-v1' or
        target['template']!=tailor.TEMPLATE or target['generation_model']!=tailor.MODEL or
        (target['generation_context'] or {}).get('mode')!='APPROVED_SKILL_ORDER'):
        raise ComparisonUnavailable('Choose a recorded generated base and tailored resume from this application')
    context=target['generation_context']
    matches=context.get('matched_skills')
    if (context.get('ordered_fact_ids')!=target['fact_ids'] or not isinstance(matches,dict) or
        any(not isinstance(labels,list) or any(not isinstance(label,str) for label in labels) for labels in matches.values())):
        raise ComparisonUnavailable('Original tailoring context is unavailable')
    before=_snapshot(store,output_dir,base);after=_snapshot(store,output_dir,target)
    return {'base':before,'tailored':after,
            **compare_facts(before['facts'],after['facts'],matches),
            'contact_changed':before['contact']!=after['contact'],
            'scope':'Selected generated revisions; uploaded original resumes are not compared. Skill matches explain ordering, not qualifications.'}
