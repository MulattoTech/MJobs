"""Generate factual, versioned local documents before application preparation."""
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
from html import escape
from pathlib import Path

from docx import Document
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import select

from packages.domain.safety import Fact, select_verified_facts
from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.dedupe.routes import persist_route_choices, plan_application_routes
from services.dedupe.source_values import require_route_review


class PreparationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@contextmanager
def uncommitted_files():
    paths = []
    try:
        yield paths
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise


def prepare(store, job_id, profile_id, output_dir, check=None, profile_version=None, route_mode="DEFAULT", route_id=None):
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
            if prior["profile_version"] == profile["version"]:
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
        kinds = {"resume": [profile["name"], profile["email"], *texts],
                 "cover-letter": [profile["name"], f"Application for {job['title']}", *texts]}
        records = []
        for kind, paragraphs in kinds.items():
            check()
            document = Document()
            for text in paragraphs:
                document.add_paragraph(text)
            docx_path = root / f"{kind}.docx"
            pdf_path = root / f"{kind}.pdf"
            staged.extend((docx_path, pdf_path))
            document.save(docx_path)
            styles = getSampleStyleSheet()
            flowables = []
            for text in paragraphs:
                flowables.extend([Paragraph(escape(text), styles["Normal"]), Spacer(1, 8)])
            SimpleDocTemplate(str(pdf_path)).build(flowables)
            records.append({"id": identity(application_id, kind, revision), "application_id": application_id,
                "kind": kind, "version": revision, "profile_version": profile["version"], "files": {"docx": relative + "/" + docx_path.name, "pdf": relative + "/" + pdf_path.name},
                "fact_ids": fact_ids, "sha256": {path.suffix[1:]: hashlib.sha256(path.read_bytes()).hexdigest() for path in (docx_path, pdf_path)},
                "template": "verified-facts-v1"})
        check()
        upsert(connection, m.applications, {"id": application_id, "job_id": job_id, "route_id": route["id"], "profile_id": profile_id,
            "profile_version": profile["version"], "document_version": revision, "state": "PREPARED", "target": "MOCK" if route["provider"] == "mock-ats" else "REAL", "fact_ids": fact_ids,
            "trace": [*(prior["trace"] if prior else []), "GENERATE_DOCUMENTS", "PREPARE_APPLICATION"], "confirmation": None, "created_at": prior["created_at"] if prior else now()}, replace=bool(prior))
        selected_route_ids = persist_route_choices(connection, application_id, route_plan)
        for record in records:
            upsert(connection, m.documents, record)
        store._audit(connection, application_id, "DOCUMENTS_PREPARED", {"fact_ids": fact_ids, "profile_version": profile["version"], "route_ids": selected_route_ids, "route_mode": route_plan["mode"]})
        return application_id
