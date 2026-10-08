"""Generate factual, versioned local documents before application preparation."""
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from docx import Document
from docx.shared import Pt, RGBColor
from pypdf import PdfReader
from sqlalchemy import select

from packages.domain.safety import Fact, select_verified_facts
from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.dedupe.routes import persist_route_choices, plan_application_routes
from services.dedupe.source_values import require_route_review
from services.applications.knowledge import application_adapter_knowledge
from services.documents import tailor
from services.documents.avoid_list import validate_content as validate_avoid_list
from services.documents.artifacts import atomic_generate, publish_directory
from services.documents.cover_letter import COVER_LETTER_TEMPLATE, build_cover_letter
from services.documents.errors import PreparationError
from services.documents.history import application_documents
from services.documents.page_fit import fit_to_page_target
from services.documents.render import render_pdf
from services.documents.validation import VALIDATOR, validate_assertions, validate_formats
from services.documents.templates import BASE_TEMPLATE, resume_layout, template_pair, contact_header, RESPONSIBILITY_EMPHASIS_TEMPLATES


def compose_paragraphs(profile, job, resume_rows, texts, template=BASE_TEMPLATE):
    return {'resume': resume_layout(profile, resume_rows, template)[0],
            'cover-letter': [profile['name'], f"Application for {job['title']}", *texts]}


@contextmanager
def uncommitted_files():
    paths = []
    try:
        yield paths
    except BaseException:
        for path in reversed(paths):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        raise


def _measure_and_fit_resume_rows(profile, job, rows, template, page_target, probe_dir,
                                 include_bullet_emphasis, explicitly_selected_ids=None):
    bullet_rows = [row for row in rows if row['kind'] in tailor.TARGET_BULLET_FACT_KINDS]
    skill_matches = tailor.matched_labels(bullet_rows, job)
    responsibility_matches = tailor.matched_responsibility_phrases(
        bullet_rows, tailor.target_responsibilities(job))
    explicitly_selected_ids = set(explicitly_selected_ids or ())
    fit_facts = []
    for row in rows:
        kind = row['kind']
        rank = (2 * len(skill_matches.get(row['fact_id'], ())) +
                len(responsibility_matches.get(row['fact_id'], ())))
        fit_facts.append({
            'fact_id': row['fact_id'],
            'kind': kind,
            'text': row['text'],
            'approved': bool(row['verified'] and row['user_approved']),
            'protected': kind in {
                'summary', 'experience', 'education', 'certification',
                'skill', 'technology_experience',
            },
            'optional_bullet': kind in {'responsibility', 'project', 'accomplishment'},
            'rank': rank,
            'explicitly_selected': row['fact_id'] in explicitly_selected_ids,
        })

    measurement_index = 0

    def measure_resume_pdf(candidate_facts):
        nonlocal measurement_index
        measurement_index += 1
        candidate_ids = {row['fact_id'] for row in candidate_facts}
        candidate_rows = [row for row in rows if row['fact_id'] in candidate_ids]
        candidate_paragraphs, candidate_headings = resume_layout(profile, candidate_rows, template)
        candidate_emphasis = tailor.emphasis(
            candidate_rows, job, include_responsibilities=include_bullet_emphasis)
        contact_count = len(contact_header(profile, template))
        candidate_fact_indices = [index for index in range(contact_count,
            len(candidate_paragraphs)) if index not in candidate_headings]
        candidate_bold_indices = {index for index, row in zip(candidate_fact_indices, candidate_rows)
            if row['fact_id'] in candidate_emphasis}
        probe_path = probe_dir / f'.resume-page-fit-attempt-{measurement_index}.pdf'
        atomic_generate(probe_path, lambda path: render_pdf(
            path, candidate_paragraphs, heading_indices=candidate_headings,
            bold_indices=candidate_bold_indices))
        try:
            return len(PdfReader(str(probe_path)).pages)
        finally:
            probe_path.unlink(missing_ok=True)

    fit_result = fit_to_page_target(
        fit_facts, page_target=page_target, measure_page_count=measure_resume_pdf)
    selected_ids = {row['fact_id'] for row in fit_result.selected_facts}
    selected_rows = [row for row in rows if row['fact_id'] in selected_ids]
    return selected_rows, {
        'target_page_count': fit_result.page_target,
        'actual_page_count': fit_result.page_count,
        'unmet_target': fit_result.measurement_available and not fit_result.target_met,
        'measurement_status': 'MEASURED' if fit_result.measurement_available else 'UNAVAILABLE',
        'attempts_used': fit_result.attempts,
        'max_attempts': 8,
        'reason': fit_result.reason,
        'omitted_facts': list(fit_result.omitted_facts),
    }


def _page_fit_api_result(page_fit):
    return {
        'page_target': page_fit['target_page_count'],
        'page_count': page_fit['actual_page_count'],
        'attempts': page_fit['attempts_used'],
        'measurement_available': page_fit['measurement_status'] == 'MEASURED',
        'target_met': not page_fit['unmet_target'] and page_fit['measurement_status'] == 'MEASURED',
        'reason': page_fit['reason'],
        'omitted_facts': list(page_fit['omitted_facts']),
    }


def preview_resume_page_fit(store, profile_id, job_id, output_dir, target_pdf_pages):
    """Render a transient measured PDF preview without creating an application or revision."""
    if type(target_pdf_pages) is not int or not 1 <= target_pdf_pages <= 20:
        raise ValueError('target_pdf_pages must be an integer from 1 through 20')
    profile = store.profile(profile_id)
    job = store.job(job_id, profile_id)
    eligible = tailor.eligible_rows(profile)
    if not eligible:
        raise PreparationError('DOCUMENT_NO_APPROVED_FACTS',
            'Approve and verify at least one sourced career fact before generating documents')
    rows = tailor.select_resume_rows(eligible, True, None)
    rows, _ = tailor.select_summary_rows(rows, job)
    rows, _ = tailor.select_bullet_rows(rows, job)
    template, _model = template_pair(True, False, False)
    include_bullet_emphasis = template in RESPONSIBILITY_EMPHASIS_TEMPLATES
    rows, _context = tailor.order_skills(rows, job,
        include_responsibilities=include_bullet_emphasis)

    preview_root = Path(output_dir).resolve() / '.page-fit-previews'
    if preview_root.is_symlink():
        raise PreparationError('DOCUMENT_PREVIEW_STORAGE_UNSAFE', 'Local preview storage is unavailable')
    preview_root.mkdir(parents=True, exist_ok=True)
    preview_id = uuid4().hex
    preview_pdf_path = preview_root / f'{preview_id}.pdf'
    selected_rows, page_fit = _measure_and_fit_resume_rows(
        profile, job, rows, template, target_pdf_pages, preview_root,
        include_bullet_emphasis)
    paragraphs, headings = resume_layout(profile, selected_rows, template)
    validate_avoid_list('\n'.join(paragraphs), profile.get('preferences'), 'resume')
    emphasized = tailor.emphasis(selected_rows, job,
        include_responsibilities=include_bullet_emphasis)
    fact_indices = [index for index in range(len(contact_header(profile, template)),
        len(paragraphs)) if index not in headings]
    bold_indices = {index for index, row in zip(fact_indices, selected_rows)
        if row['fact_id'] in emphasized}
    atomic_generate(preview_pdf_path, lambda path: render_pdf(
        path, paragraphs, heading_indices=headings, bold_indices=bold_indices))
    return {
        'page_fit': _page_fit_api_result(page_fit),
        'preview_pdf_path': str(preview_pdf_path),
        'profile_version': profile['version'],
        'job_fingerprint': tailor.target_fingerprint(job),
    }


def discard_resume_page_fit_preview(output_dir, preview):
    """Remove only a PDF preview created inside this workspace's private preview directory."""
    raw_path = preview.get('preview_pdf_path') if isinstance(preview, dict) else None
    if not isinstance(raw_path, str):
        return
    preview_root = Path(output_dir).resolve() / '.page-fit-previews'
    path = Path(raw_path)
    if (preview_root.is_symlink() or path.parent != preview_root or len(path.stem) != 32 or
            any(char not in '0123456789abcdef' for char in path.stem) or path.suffix != '.pdf'):
        return
    path.unlink(missing_ok=True)
    try:
        preview_root.rmdir()
    except OSError:
        pass


def generate_resume_page_fit_preview(store, profile_id, job_id, output_dir, preview):
    """Persist a transient preview only after the API has collected explicit review."""
    if not isinstance(preview, dict) or not isinstance(preview.get('page_fit'), dict):
        raise ValueError('resume page-fit preview is invalid')
    page_fit = preview['page_fit']
    try:
        if not page_fit.get('measurement_available'):
            raise ValueError('resume page-fit preview has no measured PDF page count')
        if type(page_fit.get('page_target')) is not int or not 1 <= page_fit['page_target'] <= 20:
            raise ValueError('resume page-fit preview target is invalid')
        profile_version = preview.get('profile_version')
        job_fingerprint = preview.get('job_fingerprint')
        if not isinstance(profile_version, str) or not profile_version or not isinstance(job_fingerprint, str):
            raise ValueError('resume page-fit preview provenance is invalid')
        current_job = store.job(job_id, profile_id)
        if tailor.target_fingerprint(current_job) != job_fingerprint:
            raise ValueError('resume page-fit preview target changed')
        application_id = prepare(
            store, job_id, profile_id, output_dir, profile_version=profile_version,
            tailor_resume=True, resume_page_target=page_fit['page_target'],
            resume_page_fit_job_fingerprint=job_fingerprint)
        rows = application_documents(store, application_id)
        resume = next(row for row in rows if row['kind'] == 'resume' and
                      row['version'] == store.application(application_id)['document_version'])
        persisted = resume['generation_context'].get('page_fit')
        if _page_fit_api_result(persisted) != page_fit:
            raise ValueError('resume page-fit output no longer matches its preview')
        return {'application_id': application_id}
    finally:
        discard_resume_page_fit_preview(output_dir, preview)


def prepare(store, job_id, profile_id, output_dir, check=None, profile_version=None, route_mode="DEFAULT", route_id=None, tailor_resume=False, tailor_fact_ids=None, tailor_fact_rewrites=None, include_resume_contact=False, cover_letter_style="professional", cover_letter_length="normal", resume_page_target=None, resume_page_fit_job_fingerprint=None):
    if type(tailor_resume) is not bool:
        raise ValueError('tailor_resume must be a boolean')
    if type(include_resume_contact) is not bool:
        raise ValueError('include_resume_contact must be a boolean')
    if resume_page_target is not None and (type(resume_page_target) is not int or
            not 1 <= resume_page_target <= 20):
        raise ValueError('resume_page_target must be an integer from 1 through 20')
    if resume_page_target is not None and not tailor_resume:
        raise ValueError('resume_page_target requires tailor_resume=true')
    if (resume_page_fit_job_fingerprint is not None and
            (not isinstance(resume_page_fit_job_fingerprint, str) or
             not resume_page_fit_job_fingerprint)):
        raise ValueError('resume_page_fit_job_fingerprint must be a nonempty string')
    resume_template,resume_model=template_pair(tailor_resume,tailor_fact_ids is not None,include_resume_contact,
        user_rephrase=tailor_fact_rewrites is not None)
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
        if not eligible:
            raise PreparationError("DOCUMENT_NO_APPROVED_FACTS", "Approve and verify at least one sourced career fact before generating documents")
        resume_rows = tailor.select_resume_rows(eligible, tailor_resume, tailor_fact_ids)
        selection_ids = [row['fact_id'] for row in resume_rows] if tailor_fact_ids is not None else None
        selection_hash = tailor.selection_fingerprint(selection_ids) if selection_ids is not None else None
        rewritten_rows, rephrase_context = (tailor.validate_user_rewrites(
            resume_rows, selection_ids, tailor_fact_rewrites) if tailor_fact_rewrites is not None
            else (resume_rows, []))
        rephrase_hash = hashlib.sha256(json.dumps(tailor_fact_rewrites,
            ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()).hexdigest() if tailor_fact_rewrites is not None else None
        check()
        job = dict(connection.execute(select(m.jobs, m.companies.c.name.label("company")).join(
            m.companies, m.companies.c.id == m.jobs.c.company_id).where(m.jobs.c.id == job_id)
            .with_for_update(of=m.jobs)).mappings().one())
        if (resume_page_fit_job_fingerprint is not None and
                tailor.target_fingerprint(job) != resume_page_fit_job_fingerprint):
            raise PreparationError('RESUME_PAGE_FIT_PREVIEW_STALE_JOB',
                'The target job changed; preview page fit again before generating documents')
        from services.company_discovery.company_context import approved_company_context_for_job
        job_attributes = dict(job.get("attributes") or {})
        job_attributes["approved_company_context"] = approved_company_context_for_job(
            connection, job_id, job["company_id"])
        job["attributes"] = job_attributes
        try:
            require_route_review(connection, job_id, route_mode, route_id)
        except ValueError as error:
            raise PreparationError(str(error), 'The selected source link differs from a preserved application route; review and select the route explicitly') from error
        routes = [dict(row) for row in connection.execute(select(m.routes).where(m.routes.c.job_id == job_id).order_by(m.routes.c.id)).mappings()]
        route_plan = plan_application_routes(routes, route_mode, route_id)
        route = route_plan["selected"][0]["preferred_route"]
        cover_letter_paragraphs, cover_letter_context = build_cover_letter(
            profile, job, profile["facts"], cover_letter_style, cover_letter_length)
        prior = connection.execute(select(m.applications).where(m.applications.c.job_id == job_id, m.applications.c.profile_id == profile_id).with_for_update()).mappings().one_or_none()
        if prior:
            prior_resume=connection.execute(select(m.documents).where(m.documents.c.application_id==prior['id'],
                m.documents.c.version==prior['document_version'],m.documents.c.kind=='resume')).mappings().one_or_none()
            expected_template=resume_template
            same_mode=prior_resume and prior_resume['template']==expected_template
            same_target=not tailor_resume or (prior_resume and (prior_resume['generation_context'] or {}).get('target_fingerprint')==tailor.target_fingerprint(job))
            same_selection=selection_ids is None or (prior_resume and (prior_resume['generation_context'] or {}).get('selection_fingerprint')==selection_hash)
            same_rephrases=(rephrase_hash is None or (prior_resume and
                (prior_resume['generation_context'] or {}).get('rephrase_fingerprint')==rephrase_hash))
            prior_page_fit=(prior_resume['generation_context'] or {}).get('page_fit') if prior_resume else None
            same_page_fit=((resume_page_target is None and prior_page_fit is None) or
                (resume_page_target is not None and prior_page_fit is not None and
                 prior_page_fit.get('target_page_count') == resume_page_target))
            prior_letter=connection.execute(select(m.documents).where(m.documents.c.application_id==prior['id'],
                m.documents.c.version==prior['document_version'],m.documents.c.kind=='cover-letter')).mappings().one_or_none()
            prior_letter_context = (prior_letter['generation_context'] or {}) if prior_letter else {}
            same_letter=bool(prior_letter and prior_letter['template']==COVER_LETTER_TEMPLATE
                and {key:value for key,value in prior_letter_context.items()
                     if key != 'claim_validation'} == cover_letter_context)
            if prior["profile_version"] == profile["version"] and same_mode and same_target and same_selection and same_rephrases and same_page_fit and same_letter and prior['state'] != 'REVIEW_REQUIRED':
                persist_route_choices(connection, prior["id"], route_plan)
                return prior["id"]
            if prior["state"] not in ("PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED"):
                raise PreparationError("DOCUMENT_HISTORY_LOCKED", "Historical or active application cannot be regenerated")
        selected = select_verified_facts([Fact(id=row["fact_id"], **{key: row[key] for key in ("text", "source", "verified", "user_approved", "confidence")})
                                          for row in eligible], [row["fact_id"] for row in eligible])
        application_id = identity(job_id, profile_id)
        revision = prior["document_version"] + 1 if prior else 1
        relative = profile["version"] + "/" + str(revision)
        root = Path(output_dir).resolve() / application_id / relative
        staging = root.with_name(f".{root.name}.staging-{uuid4().hex}")
        staging.mkdir(parents=True, exist_ok=False)
        staged.append(staging)
        fact_ids = [fact.id for fact in selected]
        texts = [fact.text for fact in selected]
        summary_adjustment=None
        bullet_adjustment=None
        if tailor_resume and tailor_fact_ids is None:
            resume_rows,summary_adjustment=tailor.select_summary_rows(resume_rows,job)
            resume_rows,bullet_adjustment=tailor.select_bullet_rows(resume_rows,job)
        include_bullet_emphasis=resume_template in RESPONSIBILITY_EMPHASIS_TEMPLATES
        ordered_rows,context=tailor.order_skills(resume_rows,job,
            include_responsibilities=include_bullet_emphasis) if tailor_resume else (resume_rows,None)
        emphasized=tailor.emphasis(ordered_rows,job,
            include_responsibilities=include_bullet_emphasis) if tailor_resume else []
        if context is not None:
            context['emphasized_fact_ids']=emphasized
            if summary_adjustment is not None:
                context['summary_adjustment']=summary_adjustment
            if bullet_adjustment is not None:
                context['bullet_adjustment']=bullet_adjustment
        if selection_ids is not None:
            context.update(selection_mode='EXPLICIT_APPROVED_FACTS',selected_fact_ids=selection_ids,
                available_fact_ids=fact_ids,omitted_fact_ids=[value for value in fact_ids if value not in set(selection_ids)],
                selection_fingerprint=selection_hash)
        if rephrase_context:
            ordered_by_id={row['fact_id']:row for row in rewritten_rows}
            ordered_rows=[dict(row, text=ordered_by_id[row['fact_id']]['text']) for row in ordered_rows]
            context['fact_rephrases']=rephrase_context
            context['rephrase_fingerprint']=rephrase_hash
        if resume_page_target is not None:
            ordered_rows, page_fit = _measure_and_fit_resume_rows(
                profile, job, ordered_rows, resume_template, resume_page_target,
                staging, include_bullet_emphasis, selection_ids)
            selected_fit_ids = {row['fact_id'] for row in ordered_rows}
            if context and 'emphasized_fact_ids' in context:
                context['emphasized_fact_ids'] = [
                    fact_id for fact_id in context['emphasized_fact_ids'] if fact_id in selected_fit_ids]
            context = context or {'mode': 'APPROVED_FACT_PAGE_FIT'}
            context['page_fit'] = page_fit
        resume_ids=[row['fact_id'] for row in ordered_rows]
        if include_resume_contact:
            context=context or {'mode':'APPROVED_CONTACT_HEADER'}
            context['contact_header']=contact_header(profile,resume_template)
        kinds = compose_paragraphs(profile, job, ordered_rows, texts, resume_template)
        kinds['cover-letter'] = cover_letter_paragraphs
        if set(kinds) != {'resume', 'cover-letter'}:
            raise PreparationError('UNSUPPORTED_CLAIM', 'The generated document set has no complete validation contract')
        for kind, paragraphs in kinds.items():
            output_path = 'resume' if kind == 'resume' else 'cover letter'
            validate_avoid_list('\n'.join(paragraphs), profile.get('preferences'), output_path)
        records = []
        generated_at = now()
        for kind, paragraphs in kinds.items():
            check()
            template = resume_template if kind=='resume' else COVER_LETTER_TEMPLATE
            current_fact_ids = resume_ids if kind=='resume' else cover_letter_context['fact_ids']
            generation_context = context if kind=='resume' else cover_letter_context
            claim_validation = validate_assertions(
                kind, paragraphs, profile, current_fact_ids, job['title'], template,
                generation_context=generation_context)
            generation_context = {**(generation_context or {}),
                                 'claim_validation': claim_validation}
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
            docx_path = staging / f"{kind}.docx"
            pdf_path = staging / f"{kind}.pdf"
            txt_path = staging / f"{kind}.txt"
            atomic_generate(docx_path, document.save)
            atomic_generate(txt_path, lambda path: path.write_text(
                '\n'.join(paragraphs) + '\n', encoding='utf-8', newline='\n'))
            atomic_generate(pdf_path, lambda path: render_pdf(
                path, paragraphs, heading_indices=headings, bold_indices=bold_indices))
            validate_formats({'docx': docx_path, 'pdf': pdf_path, 'txt': txt_path}, paragraphs)
            records.append({"id": identity(application_id, kind, revision), "application_id": application_id,
                "kind": kind, "version": revision, "profile_version": profile["version"],
                "files": {path.suffix[1:]: relative + '/' + path.name for path in (docx_path, pdf_path, txt_path)},
                "fact_ids": current_fact_ids, "sha256": {path.suffix[1:]: hashlib.sha256(path.read_bytes()).hexdigest() for path in (docx_path, pdf_path, txt_path)},
                "template": template, "created_at": generated_at,
                "generation_model": resume_model if kind=='resume' else "deterministic-job-evidence-v1",
                "generation_context": generation_context})
        check()
        publish_directory(staging, root)
        staged.append(root)
        upsert(connection, m.applications, {"id": application_id, "job_id": job_id, "route_id": route["id"], "profile_id": profile_id,
            "profile_version": profile["version"], "document_version": revision, "state": "PREPARED", "target": "MOCK" if route["provider"] == "mock-ats" else "REAL", "fact_ids": fact_ids,
            "trace": [*(prior["trace"] if prior else []), "GENERATE_DOCUMENTS", "PREPARE_APPLICATION"], "confirmation": None, "created_at": prior["created_at"] if prior else now()}, replace=bool(prior))
        selected_route_ids = persist_route_choices(connection, application_id, route_plan)
        for record in records:
            upsert(connection, m.documents, record)
        store._audit(connection, application_id, 'DOCUMENTS_VALIDATED', {'validator': VALIDATOR,
            'profile_version': profile['version'], 'documents': [
                {'id': r['id'], 'fact_ids': r['fact_ids'], 'sha256': r['sha256'],
                 'claim_validation': r['generation_context']['claim_validation']}
                for r in records]})
        store._audit(connection, application_id, "DOCUMENTS_PREPARED", {"fact_ids": fact_ids, "profile_version": profile["version"], "route_ids": selected_route_ids, "route_mode": route_plan["mode"],
            "adapter_knowledge": application_adapter_knowledge(connection, route["id"])})
        return application_id
