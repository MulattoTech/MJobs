"""Read-only comparisons of explicitly selected, immutable generated resumes."""
import hashlib
from pathlib import Path
from sqlalchemy.exc import NoResultFound

from services.documents.history import application_documents
from services.documents.validation import validate_assertions
from services.documents.errors import PreparationError
from services.documents.templates import (contract_role, resume_layout, SELECTION_TEMPLATES,
    EMPHASIS_TEMPLATES, CONTACT_TEMPLATES, RESPONSIBILITY_EMPHASIS_TEMPLATES,
    SUMMARY_TAILORING_TEMPLATES, TARGET_BULLET_TAILORING_TEMPLATES,
    REPHRASE_TEMPLATE, LEGACY_REPHRASE_TEMPLATE,
    CONTACT_PAIRS, LEGACY_CONTACT_PAIRS, contact_header)
from services.documents import tailor


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
                             if row['kind'] in ('skill','technology_experience','responsibility') and matches.get(row['fact_id'])],
        'omitted':[row for row in base if row['fact_id'] not in new],
        'added':[row for row in target if row['fact_id'] not in old]}


def read_generated_snapshot(store, output_dir, row):
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
        if row['template'] in SELECTION_TEMPLATES and (row['generation_context'] or {}).get('available_fact_ids')!=[fact['fact_id'] for fact in tailor.eligible_rows(profile)]:
            raise ComparisonUnavailable('Original selection context differs from its approved fact history')
        if not contract_role(row['template'],row['generation_model'],allow_legacy_unrecorded=True):
            raise ComparisonUnavailable('Original document generation contract is unavailable')
        facts=row['facts_used']
        legacy_rephrase_templates=(LEGACY_REPHRASE_TEMPLATE,LEGACY_CONTACT_PAIRS[LEGACY_REPHRASE_TEMPLATE][0])
        current_rewrite_templates=(REPHRASE_TEMPLATE,CONTACT_PAIRS[REPHRASE_TEMPLATE][0])
        if row['template'] in (*legacy_rephrase_templates,*current_rewrite_templates):
            by_id={fact['fact_id']:fact for fact in facts}
            source=[dict(by_id[identifier]) for identifier in row['fact_ids']]
            saved=(row['generation_context'] or {}).get('fact_rephrases')
            facts=(tailor.apply_saved_user_rewrites(source,row['fact_ids'],saved)
                if row['template'] in current_rewrite_templates else
                tailor.apply_saved_user_rephrases(source,row['fact_ids'],saved))
        paragraphs,_=resume_layout(profile,facts,row['template'])
        header=contact_header(profile,row['template'])
        if row['template'] in CONTACT_TEMPLATES and (row['generation_context'] or {}).get('contact_header')!=header:
            raise ComparisonUnavailable('Original contact header differs from its saved profile snapshot')
        validate_assertions('resume',paragraphs,profile,row['fact_ids'],template=row['template'],
            generation_context=row['generation_context'])
        if text!='\n'.join(paragraphs)+'\n':
            raise ComparisonUnavailable('Stored text differs from its original fact history')
    except (OSError,UnicodeError,PreparationError,NoResultFound) as error:
        raise ComparisonUnavailable('Original document or approved fact history is unavailable') from error
    rewrite_templates=(REPHRASE_TEMPLATE,CONTACT_PAIRS[REPHRASE_TEMPLATE][0],
        LEGACY_REPHRASE_TEMPLATE,LEGACY_CONTACT_PAIRS[LEGACY_REPHRASE_TEMPLATE][0])
    facts_used=facts if row['template'] in rewrite_templates else row['facts_used']
    facts=[{key:fact[key] for key in ('fact_id','kind','text')} for fact in facts_used]
    return {'id':row['id'],'version':row['version'],'profile_version':row['profile_version'],
            'text':text,'sha256':row['sha256']['txt'],'facts':facts,'contact':header}


def tailored_context(target, target_source_facts=None):
    if (not target or target['kind']!='resume' or
        contract_role(target['template'],target['generation_model'])!='TAILORED' or
        (target['generation_context'] or {}).get('mode')!='APPROVED_SKILL_ORDER'):
        raise ComparisonUnavailable('Choose a recorded tailored resume from this application')
    context=target['generation_context']
    matches=context.get('matched_skills')
    if (context.get('ordered_fact_ids')!=target['fact_ids'] or not isinstance(matches,dict) or
        any(not isinstance(labels,list) or any(not isinstance(label,str) for label in labels) for labels in matches.values())):
        raise ComparisonUnavailable('Original tailoring context is unavailable')
    if target['template'] in EMPHASIS_TEMPLATES:
        skills=context.get('job_skills')
        if (not isinstance(skills,list) or any(not isinstance(value,str) for value in skills) or
            context.get('emphasized_fact_ids')!=tailor.emphasis(target['facts_used'],{'skills':skills},
                include_responsibilities=target['template'] in RESPONSIBILITY_EMPHASIS_TEMPLATES) or
            matches!=tailor.matched_labels([row for row in target['facts_used'] if row['kind'] in (
                ('skill','technology_experience','responsibility') if target['template'] in RESPONSIBILITY_EMPHASIS_TEMPLATES
                else ('skill','technology_experience'))],{'skills':skills})):
            raise ComparisonUnavailable('Original technology emphasis context is unavailable')
    if target['template'] in SELECTION_TEMPLATES:
        selected=context.get('selected_fact_ids');available=context.get('available_fact_ids')
        if (context.get('selection_mode')!='EXPLICIT_APPROVED_FACTS' or
            not isinstance(selected,list) or not selected or not isinstance(available,list) or
            any(not isinstance(value,str) for value in [*selected,*available]) or
            len(set(selected))!=len(selected) or len(set(available))!=len(available) or
            selected!=context.get('original_fact_ids') or set(selected)!=set(target['fact_ids']) or
            not set(selected).issubset(available) or
            context.get('omitted_fact_ids')!=[value for value in available if value not in selected] or
            context.get('selection_fingerprint')!=tailor.selection_fingerprint(selected)):
            raise ComparisonUnavailable('Original approved selection context is unavailable')
    if target['template'] in SUMMARY_TAILORING_TEMPLATES:
        adjustment=context.get('summary_adjustment')
        if not isinstance(adjustment,dict):
            raise ComparisonUnavailable('Original summary adjustment context is unavailable')
        original=adjustment.get('original_summary_fact_ids');selected=adjustment.get('selected_summary_fact_ids')
        omitted=adjustment.get('omitted_summary_fact_ids');summary_matches=adjustment.get('matched_skills')
        target_summaries=[row['fact_id'] for row in target['facts_used'] if row['kind']=='summary']
        if (adjustment.get('mode')!='EXACT_TARGET_SKILL_SUMMARY_FACTS' or
            type(adjustment.get('adjusted')) is not bool or not isinstance(original,list) or
            not isinstance(selected,list) or not isinstance(omitted,list) or
            any(not isinstance(value,str) for value in [*original,*selected,*omitted]) or
            len(set(original))!=len(original) or len(set(selected))!=len(selected) or
            len(set(omitted))!=len(omitted) or set(selected)&set(omitted) or
            set(original)!=(set(selected)|set(omitted)) or selected!=target_summaries or
            not isinstance(summary_matches,dict) or set(summary_matches)!=set(original) or
            any(not isinstance(labels,list) or any(not isinstance(label,str) for label in labels)
                for labels in summary_matches.values()) or
            (adjustment['adjusted'] and (not selected or any(not summary_matches[value] for value in selected)
                or any(summary_matches[value] for value in omitted))) or
            (not adjustment['adjusted'] and (selected!=original or omitted or any(summary_matches.values())))):
            raise ComparisonUnavailable('Original summary adjustment context is unavailable')
    if target['template'] in TARGET_BULLET_TAILORING_TEMPLATES:
        adjustment=context.get('bullet_adjustment')
        if not isinstance(adjustment,dict):
            raise ComparisonUnavailable('Original target-bullet adjustment context is unavailable')
        skills=context.get('job_skills')
        recorded_original=adjustment.get('original_bullet_fact_ids')
        recorded_selected=adjustment.get('selected_bullet_fact_ids')
        recorded_omitted=adjustment.get('omitted_bullet_fact_ids')
        recorded_matches=adjustment.get('matched_skills')
        target_bullets=[row['fact_id'] for row in target['facts_used']
                        if row['kind'] in tailor.TARGET_BULLET_FACT_KINDS]
        valid_ids=lambda value: isinstance(value,list) and all(isinstance(item,str) for item in value) and len(set(value))==len(value)
        valid_matches=(isinstance(recorded_matches,dict) and
            all(isinstance(labels,list) and all(isinstance(label,str) for label in labels)
                for labels in recorded_matches.values()))
        if (adjustment.get('mode')!='EXACT_TARGET_SKILL_BULLET_FACTS' or
            type(adjustment.get('adjusted')) is not bool or
            not isinstance(skills,list) or any(not isinstance(value,str) for value in skills) or
            not valid_ids(recorded_original) or not valid_ids(recorded_selected) or
            not valid_ids(recorded_omitted) or not valid_matches or
            set(recorded_selected)&set(recorded_omitted) or
            set(recorded_original)!=(set(recorded_selected)|set(recorded_omitted)) or
            recorded_selected!=[identifier for identifier in recorded_original if identifier in set(recorded_selected)] or
            recorded_omitted!=[identifier for identifier in recorded_original if identifier in set(recorded_omitted)] or
            set(recorded_matches)!=set(recorded_original) or target_bullets!=recorded_selected):
            raise ComparisonUnavailable('Original target-bullet adjustment context is unavailable')
        if target_source_facts is not None:
            original=[row['fact_id'] for row in target_source_facts if row['kind'] in tailor.TARGET_BULLET_FACT_KINDS]
            bullet_rows=[row for row in target_source_facts if row['kind'] in tailor.TARGET_BULLET_FACT_KINDS]
            matches=tailor.matched_labels(bullet_rows,{'skills':skills})
            matched=[identifier for identifier in original if matches[identifier]]
            selected=matched if matched else original
            omitted=[identifier for identifier in original if identifier not in set(selected)]
            if (adjustment['adjusted'] != bool(matched) or recorded_original != original or
                recorded_selected != selected or recorded_omitted != omitted or recorded_matches != matches):
                raise ComparisonUnavailable('Original target-bullet adjustment context is unavailable')
        else:
            # Uploaded-source comparisons do not supply the target profile's full source
            # rows here. Validate the saved partition and recompute included bullets.
            target_rows=[row for row in target['facts_used'] if row['kind'] in tailor.TARGET_BULLET_FACT_KINDS]
            target_matches=tailor.matched_labels(target_rows,{'skills':skills})
            if (any(recorded_matches[identifier] != target_matches[identifier] for identifier in target_bullets) or
                (adjustment['adjusted'] and (not recorded_selected or
                    any(not recorded_matches[identifier] for identifier in recorded_selected) or
                    any(recorded_matches[identifier] for identifier in recorded_omitted))) or
                (not adjustment['adjusted'] and (recorded_selected != recorded_original or
                    recorded_omitted or any(recorded_matches.values())))):
                raise ComparisonUnavailable('Original target-bullet adjustment context is unavailable')
    return context


def compare_resumes(store, output_dir, application_id, base_document_id, tailored_document_id):
    rows={row['id']:row for row in application_documents(store,application_id)}
    base=rows.get(base_document_id);target=rows.get(tailored_document_id)
    if (not base or base['kind']!='resume' or contract_role(base['template'],base['generation_model'],allow_legacy_unrecorded=True)!='BASE'):
        raise ComparisonUnavailable('Choose a recorded generated base and tailored resume from this application')
    before=read_generated_snapshot(store,output_dir,base);after=read_generated_snapshot(store,output_dir,target)
    # Tailoring decisions are bound to the target's generation-time profile snapshot,
    # which may differ from the older generated base being compared.
    target_profile=store.profile(target['profile_id'],target['profile_version'])
    context=tailored_context(target,tailor.eligible_rows(target_profile))
    return {'base':before,'tailored':after,
            **compare_facts(before['facts'],after['facts'],context['matched_skills']),
            'contact_changed':before['contact']!=after['contact'],
            'scope':'Selected generated revisions; uploaded original resumes are not compared. Exact job-skill mentions explain ordering and emphasis, not qualifications.'}
