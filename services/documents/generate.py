"""Generate factual, versioned local documents before application preparation."""
from dataclasses import asdict
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


def prepare(store, job_id, profile_id, output_dir, check=None, profile_version=None):
    check = check or (lambda: None)
    with store.engine.begin() as connection:
        # A profile row serializes preparation and submission reservations.
        # SQLite has no row locks; use its explicit write transaction in tests.
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(select(m.profiles).where(m.profiles.c.id == profile_id).with_for_update()).one()
        profile = store.profile(profile_id, profile_version)
        check()
        prior = connection.execute(select(m.applications.c.id).where(m.applications.c.job_id == job_id, m.applications.c.profile_id == profile_id)).scalar_one_or_none()
        if prior:
            return prior
        job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == job_id)).mappings().one())
        route = dict(connection.execute(select(m.routes).where(m.routes.c.job_id == job_id)).mappings().one())
        selected = select_verified_facts([Fact(id=row["fact_id"], **{key: row[key] for key in ("text", "source", "verified", "user_approved", "confidence")})
                                          for row in profile["facts"]], [row["fact_id"] for row in profile["facts"]])
        application_id = identity(job_id, profile_id)
        root = Path(output_dir).resolve() / application_id / profile["version"]
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
            document.save(docx_path)
            styles = getSampleStyleSheet()
            flowables = []
            for text in paragraphs:
                flowables.extend([Paragraph(escape(text), styles["Normal"]), Spacer(1, 8)])
            SimpleDocTemplate(str(pdf_path)).build(flowables)
            records.append({"id": identity(application_id, kind, 1), "application_id": application_id,
                "kind": kind, "version": 1, "files": {"docx": profile["version"] + "/" + docx_path.name, "pdf": profile["version"] + "/" + pdf_path.name},
                "fact_ids": fact_ids, "sha256": {path.suffix[1:]: hashlib.sha256(path.read_bytes()).hexdigest() for path in (docx_path, pdf_path)},
                "template": "verified-facts-v1"})
        check()
        upsert(connection, m.applications, {"id": application_id, "job_id": job_id, "route_id": route["id"], "profile_id": profile_id,
            "profile_version": profile["version"], "state": "PREPARED", "target": "MOCK", "fact_ids": fact_ids,
            "trace": ["GENERATE_DOCUMENTS", "PREPARE_APPLICATION"], "confirmation": None, "created_at": now()})
        for record in records:
            upsert(connection, m.documents, record)
        store._audit(connection, application_id, "DOCUMENTS_PREPARED", {"fact_ids": fact_ids, "profile_version": profile["version"]})
        return application_id
