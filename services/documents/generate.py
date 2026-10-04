"""Generate factual, versioned local documents before application preparation."""
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
from pathlib import Path

from docx import Document
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


def compose_paragraphs(profile, job, resume_rows, texts):
    return {'resume': [profile['name'], profile['email'], *[row['text'] for row in resume_rows]],
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


def prepare(store, job_id, profile_id, output_dir, check=None, profile_version=None, route_mode="DEFAULT", route_id=None, tailor_resume=False):
    if type(tailor_resume) is not bool:
        raise ValueError('tailor_resume must be a boolean')
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
            expected_template=tailor.TEMPLATE if tailor_resume else 'verified-facts-v1'
            same_mode=prior_resume and prior_resume['template']==expected_template
            same_target=not tailor_resume or (prior_resume and (prior_resume['generation_context'] or {}).get('target_fingerprint')==tailor.target_fingerprint(job))
            if prior["profile_version"] == profile["version"] and same_mode and same_target and prior['state'] != 'REVIEW_REQUIRED':
                persist_route_choices(connection, prior["id"], route_plan)
                return prior["id"]
            if prior["state"] not in ("PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED"):
                raise PreparationError("DOCUMENT_HISTORY_LOCKED", "Historical or active application cannot be regenerated")
        eligible = [row for row in profile["facts"] if row["verified"] and row["user_approved"] and row["confidence"] >= 80 and row["source"].strip()]
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
        ordered_rows,context=tailor.order_skills(eligible,job) if tailor_resume else (eligible,None)
        resume_ids=[row['fact_id'] for row in ordered_rows]
        kinds = compose_paragraphs(profile, job, ordered_rows, texts)
        if set(kinds) != {'resume', 'cover-letter'}:
            raise PreparationError('UNSUPPORTED_CLAIM', 'The generated document set has no complete validation contract')
        records = []
        generated_at = now()
        for kind, paragraphs in kinds.items():
            check()
            validate_assertions(kind, paragraphs, profile, resume_ids if kind=='resume' else fact_ids, job['title'])
            document = Document()
            for text in paragraphs:
                document.add_paragraph(text)
            docx_path = root / f"{kind}.docx"
            pdf_path = root / f"{kind}.pdf"
            txt_path = root / f"{kind}.txt"
            staged.extend((docx_path, pdf_path, txt_path))
            document.save(docx_path)
            txt_path.write_text('\n'.join(paragraphs) + '\n', encoding='utf-8', newline='\n')
            render_pdf(pdf_path, paragraphs)
            validate_formats({'docx': docx_path, 'pdf': pdf_path, 'txt': txt_path}, paragraphs)
            records.append({"id": identity(application_id, kind, revision), "application_id": application_id,
                "kind": kind, "version": revision, "profile_version": profile["version"],
                "files": {path.suffix[1:]: relative + '/' + path.name for path in (docx_path, pdf_path, txt_path)},
                "fact_ids": resume_ids if kind=='resume' else fact_ids, "sha256": {path.suffix[1:]: hashlib.sha256(path.read_bytes()).hexdigest() for path in (docx_path, pdf_path, txt_path)},
                "template": tailor.TEMPLATE if kind=='resume' and tailor_resume else "verified-facts-v1", "created_at": generated_at,
                "generation_model": tailor.MODEL if kind=='resume' and tailor_resume else "deterministic-fact-copy-v1",
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
