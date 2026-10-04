"""Generate factual, versioned local documents before application preparation."""
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor
from sqlalchemy import select

from packages.domain.safety import Fact, select_verified_facts
from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.dedupe.routes import persist_route_choices, plan_application_routes
from services.dedupe.source_values import require_route_review
from services.applications.knowledge import application_adapter_knowledge
from services.documents import tailor
from services.documents.errors import PreparationError
from services.documents.render import render_pdf
from services.documents.validation import VALIDATOR, validate_assertions, validate_formats
from services.documents.templates import BASE_TEMPLATE, resume_layout, template_pair, contact_header


def compose_paragraphs(profile, job, resume_rows, texts, template=BASE_TEMPLATE):
    return {'resume': resume_layout(profile, resume_rows, template)[0],
            'cover-letter': [profile['name'], f"Application for {job['title']}", *texts]}


@contextmanager
def uncommitted_files():
    paths = []
    try:
        yield paths
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise


def prepare(store, job_id, profile_id, output_dir, check=None, profile_version=None, route_mode="DEFAULT", route_id=None, tailor_resume=False, tailor_fact_ids=None, include_resume_contact=False):
    if type(tailor_resume) is not bool:
        raise ValueError('tailor_resume must be a boolean')
    if type(include_resume_contact) is not bool:
        raise ValueError('include_resume_contact must be a boolean')
    resume_template,resume_model=template_pair(tailor_resume,tailor_fact_ids is not None,include_resume_contact)
    check = check or (lambda: None)
    with uncommitted_files() as staged, store.engine.begin() as connection:
        # A profile row serializes preparation and submission reservations.
        # SQLite has no row locks; use its explicit write transaction in tests.
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = connection.execute(select(m.profiles).where(m.profiles.c.id == profile_id).with_for_update()).mappings().one()
        if profile_version and profile_version != current["version"]:
            raise PreparationError("DOCUMENT_PROFILE_CHANGED", "Profile changed; prepare documents using its current version")
        profile = store.profile(profile_id, profile_version)
        eligible = tailor.eligible_rows(profile)
        resume_rows = tailor.select_resume_rows(eligible, tailor_resume, tailor_fact_ids)
        selection_ids = [row['fact_id'] for row in resume_rows] if tailor_fact_ids is not None else None
        selection_hash = tailor.selection_fingerprint(selection_ids) if selection_ids is not None else None
        check()
        job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == job_id).with_for_update()).mappings().one())
        try:
            require_route_review(connection, job_id, route_mode, route_id)
        except ValueError as error:
            raise PreparationError(str(error), 'The selected source link differs from a preserved application route; review and select the route explicitly') from error
        routes = [dict(row) for row in connection.execute(select(m.routes).where(m.routes.c.job_id == job_id).order_by(m.routes.c.id)).mappings()]
        route_plan = plan_application_routes(routes, route_mode, route_id)
        route = route_plan["selected"][0]["preferred_route"]
        prior = connection.execute(select(m.applications).where(m.applications.c.job_id == job_id, m.applications.c.profile_id == profile_id).with_for_update()).mappings().one_or_none()
        if prior:
            prior_resume=connection.execute(select(m.documents).where(m.documents.c.application_id==prior['id'],
                m.documents.c.version==prior['document_version'],m.documents.c.kind=='resume')).mappings().one_or_none()
            expected_template=resume_template
            same_mode=prior_resume and prior_resume['template']==expected_template
            same_target=not tailor_resume or (prior_resume and (prior_resume['generation_context'] or {}).get('target_fingerprint')==tailor.target_fingerprint(job))
            same_selection=selection_ids is None or (prior_resume and (prior_resume['generation_context'] or {}).get('selection_fingerprint')==selection_hash)
            if prior["profile_version"] == profile["version"] and same_mode and same_target and same_selection and prior['state'] != 'REVIEW_REQUIRED':
                persist_route_choices(connection, prior["id"], route_plan)
                return prior["id"]
            if prior["state"] not in ("PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED"):
                raise PreparationError("DOCUMENT_HISTORY_LOCKED", "Historical or active application cannot be regenerated")
        if not eligible:
            raise PreparationError("DOCUMENT_NO_APPROVED_FACTS", "Approve and verify at least one sourced career fact before generating documents")
        selected = select_verified_facts([Fact(id=row["fact_id"], **{key: row[key] for key in ("text", "source", "verified", "user_approved", "confidence")})
                                          for row in eligible], [row["fact_id"] for row in eligible])
        application_id = identity(job_id, profile_id)
        revision = prior["document_version"] + 1 if prior else 1
        relative = profile["version"] + "/" + str(revision)
        root = Path(output_dir).resolve() / application_id / relative
        root.mkdir(parents=True, exist_ok=True)
        fact_ids = [fact.id for fact in selected]
        texts = [fact.text for fact in selected]
        ordered_rows,context=tailor.order_skills(resume_rows,job) if tailor_resume else (resume_rows,None)
        emphasized=tailor.emphasis(ordered_rows,job) if tailor_resume else []
        if context is not None:
            context['emphasized_fact_ids']=emphasized
        if selection_ids is not None:
            context.update(selection_mode='EXPLICIT_APPROVED_FACTS',selected_fact_ids=selection_ids,
                available_fact_ids=fact_ids,omitted_fact_ids=[value for value in fact_ids if value not in set(selection_ids)],
                selection_fingerprint=selection_hash)
        resume_ids=[row['fact_id'] for row in ordered_rows]
        if include_resume_contact:
            context=context or {'mode':'APPROVED_CONTACT_HEADER'}
            context['contact_header']=contact_header(profile,resume_template)
        kinds = compose_paragraphs(profile, job, ordered_rows, texts, resume_template)
        if set(kinds) != {'resume', 'cover-letter'}:
            raise PreparationError('UNSUPPORTED_CLAIM', 'The generated document set has no complete validation contract')
        records = []
        generated_at = now()
        for kind, paragraphs in kinds.items():
            check()
            template = resume_template if kind=='resume' else 'verified-facts-v1'
            validate_assertions(kind, paragraphs, profile, resume_ids if kind=='resume' else fact_ids, job['title'], template)
            headings = resume_layout(profile, ordered_rows, template)[1] if kind=='resume' else set()
            fact_indices=[index for index in range(len(contact_header(profile,template)),len(paragraphs)) if index not in headings] if kind=='resume' else []
            bold_indices={index for index,row in zip(fact_indices,ordered_rows) if row['fact_id'] in emphasized}
            document = Document()
            document.styles['Normal'].font.name = 'Arial'
            document.styles['Normal'].font.size = Pt(11)
            document.styles['Heading 1'].font.name = 'Arial'
            document.styles['Heading 1'].font.size = Pt(13)
            document.styles['Heading 1'].font.color.rgb = RGBColor(0,0,0)
            for index,text in enumerate(paragraphs):
                paragraph=document.add_paragraph(style='Heading 1' if index in headings else 'Normal')
                run=paragraph.add_run(text)
                if index in bold_indices:run.bold=True
            docx_path = root / f"{kind}.docx"
            pdf_path = root / f"{kind}.pdf"
            txt_path = root / f"{kind}.txt"
            staged.extend((docx_path, pdf_path, txt_path))
            document.save(docx_path)
            txt_path.write_text('\n'.join(paragraphs) + '\n', encoding='utf-8', newline='\n')
            render_pdf(pdf_path, paragraphs, heading_indices=headings, bold_indices=bold_indices)
            validate_formats({'docx': docx_path, 'pdf': pdf_path, 'txt': txt_path}, paragraphs)
            records.append({"id": identity(application_id, kind, revision), "application_id": application_id,
                "kind": kind, "version": revision, "profile_version": profile["version"],
                "files": {path.suffix[1:]: relative + '/' + path.name for path in (docx_path, pdf_path, txt_path)},
                "fact_ids": resume_ids if kind=='resume' else fact_ids, "sha256": {path.suffix[1:]: hashlib.sha256(path.read_bytes()).hexdigest() for path in (docx_path, pdf_path, txt_path)},
                "template": template, "created_at": generated_at,
                "generation_model": resume_model if kind=='resume' else "deterministic-fact-copy-v1",
                "generation_context": context if kind=='resume' else None})
        check()
        upsert(connection, m.applications, {"id": application_id, "job_id": job_id, "route_id": route["id"], "profile_id": profile_id,
            "profile_version": profile["version"], "document_version": revision, "state": "PREPARED", "target": "MOCK" if route["provider"] == "mock-ats" else "REAL", "fact_ids": fact_ids,
            "trace": [*(prior["trace"] if prior else []), "GENERATE_DOCUMENTS", "PREPARE_APPLICATION"], "confirmation": None, "created_at": prior["created_at"] if prior else now()}, replace=bool(prior))
        selected_route_ids = persist_route_choices(connection, application_id, route_plan)
        for record in records:
            upsert(connection, m.documents, record)
        store._audit(connection, application_id, 'DOCUMENTS_VALIDATED', {'validator': VALIDATOR,
            'profile_version': profile['version'], 'documents': [
                {'id': r['id'], 'fact_ids': r['fact_ids'], 'sha256': r['sha256']} for r in records]})
        store._audit(connection, application_id, "DOCUMENTS_PREPARED", {"fact_ids": fact_ids, "profile_version": profile["version"], "route_ids": selected_route_ids, "route_mode": route_plan["mode"],
            "adapter_knowledge": application_adapter_knowledge(connection, route["id"])})
        return application_id
