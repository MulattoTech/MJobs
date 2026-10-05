"""Profile-owned library projections and revision-scoped original downloads."""
from sqlalchemy import select
from packages.persistence import models as m
from services.documents.uploads import list_resumes, read_resume
from services.documents.history import application_documents
from services.documents.templates import contract_role


def document_library(store, profile_id):
    uploaded = list_resumes(store, profile_id, document_kind=None)
    with store.engine.connect() as connection:
        applications = connection.execute(select(m.applications.c.id, m.jobs.c.title,
            m.companies.c.name.label('company')).join(m.jobs, m.jobs.c.id == m.applications.c.job_id).join(
            m.companies, m.companies.c.id == m.jobs.c.company_id).where(
            m.applications.c.profile_id == profile_id).order_by(m.applications.c.created_at.desc())).mappings().all()
    generated = []
    for application in applications:
        for row in application_documents(store, application['id']):
            row['document_kind'] = ('APPLICATION_ANSWER_EXPORT' if row['kind'] == 'application-answer-export'
                                    else 'GENERATED_RESUME' if row['kind'] == 'resume' else 'COVER_LETTER')
            if row['kind']=='resume' and contract_role(row['template'],row['generation_model'])=='TAILORED' and (row['generation_context'] or {}).get('mode')=='APPROVED_SKILL_ORDER':
                row['document_kind']='TAILORED_RESUME'
            row['job_title'] = application['title']
            row['company'] = application['company']
            generated.append(row)
    return {'uploaded':uploaded, 'generated':generated}


def read_library_document(store, output_dir, profile_id, revision_id):
    with store.engine.connect() as connection:
        revision = connection.execute(select(m.resume_revisions).where(
            m.resume_revisions.c.profile_id == profile_id, m.resume_revisions.c.id == revision_id)).mappings().one()
    return read_resume(store, output_dir, profile_id, revision['upload_id'], document_kind=revision['document_kind'])
