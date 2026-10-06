"""Compare extracted original text without converting it to approved facts."""
from collections import defaultdict,deque
import re
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from packages.persistence import models as m
from services.career_profile.extraction import extract_resume_text,resume_heading_kind
from services.documents.comparison import ComparisonUnavailable,compare_facts,read_generated_snapshot,tailored_context
from services.documents.history import application_documents
from services.documents.uploads import read_resume,UploadIntegrityError
from services.discovery.skill_taxonomy import canonical_skill_id

MAX_COMPARISON_ITEMS=1000
BULLET_KINDS={'experience','responsibility','project','accomplishment','education','certification','technology_experience'}
UNCLASSIFIED_HEADINGS={'awards','honors','honours','interests','hobbies','languages','references',
    'publications','volunteering','volunteer experience','volunteer work','memberships','affiliations',
    'additional information','personal information','community involvement'}


def _key(text):
    return ' '.join(re.sub(r'^[•*\-]\s+','',text.strip()).split())


def _skill_parts(text):
    return re.split(r'\s*(?:[,;|•]|\r?\n)\s*',text)


def source_items(text):
    """Preserve occurrences; only explicit headings label source sections."""
    items=[];headings=set();uncertain=set();current='unclassified'
    for raw in text.splitlines():
        line=raw.strip()
        if not line:continue
        heading=resume_heading_kind(line)
        if heading:
            current=heading;headings.add(heading);continue
        label=line.rstrip(':').strip().casefold()
        explicit_unknown=label in UNCLASSIFIED_HEADINGS
        ambiguous=(line.endswith(':') or (len(line)<=80 and len(line.split())<=6 and
            not re.search(r'[\d@,;|•+*]',line) and (line.istitle() or line.isupper())))
        if current=='skill' and canonical_skill_id(line):ambiguous=False
        if explicit_unknown or (current!='unclassified' and ambiguous):
            if not explicit_unknown:uncertain.add(current)
            current='unclassified'
        parts=_skill_parts(line) if current=='skill' else [line]
        for part in parts:
            value=re.sub(r'^[•*\-]\s+','',part).strip()
            if not value:continue
            items.append({'fact_id':f'source:{len(items)}','kind':current,'text':value})
            if len(items)>MAX_COMPARISON_ITEMS:
                raise ComparisonUnavailable('Original resume exceeds the bounded comparison item limit')
    return items,headings,uncertain


def _section_change(before,after,kinds,identifier,available):
    if not available:return []
    old=[row['text'] for row in before if row['kind'] in kinds]
    new=[row['text'] for row in after if row['kind'] in kinds]
    if [_key(value) for value in old]==[_key(value) for value in new]:return []
    def side(lines):
        return {'fact_id':identifier,'kind':'section','text':'\n'.join(lines)} if lines else None
    return [{'fact_id':identifier,'before':side(old),'after':side(new)}]


def compare_uploaded_resume(store,output_dir,application_id,base_revision_id,tailored_document_id):
    application=store.application(application_id)
    rows={row['id']:row for row in application_documents(store,application_id)}
    target=rows.get(tailored_document_id);context=tailored_context(target)
    after=read_generated_snapshot(store,output_dir,target)
    with store.engine.connect() as c:
        revision=c.execute(select(m.resume_revisions).where(m.resume_revisions.c.id==base_revision_id,
            m.resume_revisions.c.profile_id==application['profile_id'],m.resume_revisions.c.document_kind=='BASE_RESUME')).mappings().one_or_none()
    if revision is None:
        raise ComparisonUnavailable('Choose an uploaded base resume revision owned by this application profile')
    try:
        upload,data=read_resume(store,output_dir,application['profile_id'],revision['upload_id'])
        extraction=extract_resume_text(data,upload['format'])
    except (UploadIntegrityError,NoResultFound,ValueError,OSError) as error:
        raise ComparisonUnavailable('Original uploaded resume is unavailable or failed its integrity/extraction checks') from error
    if extraction['status']!='READY_FOR_REVIEW':
        raise ComparisonUnavailable('Original resume text is unavailable; local extraction or OCR could not recover it')
    source,headings,uncertain=source_items(extraction['text'])
    destination=[{'fact_id':'contact:'+str(i),'kind':'contact','text':value} for i,value in enumerate(after['contact'])]
    for row in after['facts']:
        parts=_skill_parts(row['text']) if row['kind']=='skill' else [row['text']]
        for index,value in enumerate(parts):
            if not value.strip():continue
            destination.append({**row,'text':value,'fact_id':f"fact:{row['fact_id']}:{index}"})
            if len(destination)>MAX_COMPARISON_ITEMS:
                raise ComparisonUnavailable('Generated resume exceeds the bounded comparison item limit')
    matches=defaultdict(deque)
    for row in destination:matches[_key(row['text'])].append(row['fact_id'])
    # Only exact presentation-normalized occurrences share comparison identity.
    # No user fact ID is inferred from a paraphrase or an OCR approximation.
    aligned=[]
    for row in source:
        identifiers=matches[_key(row['text'])]
        aligned.append({**row,'fact_id':identifiers.popleft() if identifiers else row['fact_id']})
    diff=compare_facts(aligned,destination,{})
    diff['emphasized_skills']=[dict(row,job_skills=context['matched_skills'][row['fact_id']]) for row in after['facts']
        if row['kind'] in ('skill','technology_experience','responsibility') and context['matched_skills'].get(row['fact_id'])]
    summary_available='summary' in headings and 'summary' not in uncertain
    bullets_available=bool(headings&BULLET_KINDS) and not bool(uncertain&BULLET_KINDS)
    diff['summary_changes']=_section_change(source,destination,{'summary'},'summary-section',summary_available)
    diff['bullet_changes']=_section_change(source,destination,BULLET_KINDS,'bullet-sections',bullets_available)
    before={'id':revision['id'],'version':revision['version'],'source_kind':'UPLOADED',
        'filename':upload['filename'],'format':upload['format'],'sha256':upload['sha256'],
        'profile_id':application['profile_id'],'text':extraction['text'],
        'extraction':{key:extraction[key] for key in ('status','warnings','used_ocr')}}
    return {'base':before,'tailored':after,**diff,'contact_changed':None,
        'section_availability':{'summary_changes':summary_available,'bullet_changes':bullets_available},
        'scope':'Uploaded text is unreviewed. Comparison uses exact extracted text with whitespace, bullet markers and skill-list separators normalized. Unmatched text is not proof of semantic omission; OCR and reformatting may explain differences. Known headings are excluded from content matching and remain visible in the original text. Ambiguous heading candidates remain unclassified; unconventional section layouts may require review of the original.'}
