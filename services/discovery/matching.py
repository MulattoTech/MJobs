"""Recalculate deterministic matches against stored jobs without provider requests."""
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.company_discovery.requests import record_discovery_requests


def recalculate(store, profile_id, profile_version=None, check=None):
    check = check or (lambda: None)
    profile = store.profile(profile_id, profile_version)
    usable = {row["text"].strip().casefold() for row in profile["facts"] if row["kind"] == "skill"
              and row["verified"] and row["user_approved"] and row["confidence"] >= 80 and row["source"].strip()}
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        job_ids = []
        for job in connection.execute(select(m.jobs).order_by(m.jobs.c.id)).mappings():
            check()
            detected = {skill.casefold() for skill in job["skills"]}
            score = round(100 * len(detected & usable) / len(detected), 2) if detected else 0
            method = "structured_required_skills" if job["attributes"].get("fictional") else "description_skill_mentions"
            upsert(connection, m.matches, {"id": identity(job["id"], profile_id, profile["version"]), "job_id": job["id"],
                "profile_id": profile_id, "profile_version": profile["version"], "score": score, "missing": sorted(detected - usable),
                "components": {"method": method, "matched": sorted(detected & usable), "detected": sorted(detected),
                    "limitation": "Description mentions are heuristic evidence, not verified mandatory requirements or a full qualification assessment." if method == "description_skill_mentions" else None}}, replace=True)
            job_ids.append(job['id'])
        record_discovery_requests(connection, job_ids, profile)
        store._audit(connection, profile_id, "MATCHES_RECALCULATED", {"profile_version": profile["version"]})
