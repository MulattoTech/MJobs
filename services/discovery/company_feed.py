"""Normalize and persist discovered employer feed rows through canonical jobs."""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.discovery.search_semantics import (
    classify_location, normalize_employment_type, normalize_experience_level,
    normalize_title, parse_education_requirement, parse_salary, parse_timestamp,
)
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.negative_filters import parse_negative_conditions
from services.discovery.sources.collect import persist_posting
from services.discovery.sources.greenhouse import plain_text


def _application_url(item, strategy, endpoint):
    value = item.get("url") or item.get("external_url") if strategy == "PUBLIC_JSON" else item.get("link")
    target = urljoin(endpoint, str(value or "").strip())
    try:
        parsed = urlsplit(target)
        if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        parsed.port
    except ValueError:
        return None
    return target[:2000] if len(target) <= 2000 else None


def import_company_feed_jobs(store, *, company_id, source_id, provider, tenant,
                             endpoint, strategy, items, profile_id=None, profile_version=None,
                             check=None):
    """Persist bounded public feed rows and optional profile matches idempotently."""
    check = check or (lambda: None)
    profile = store.profile(profile_id, profile_version) if profile_id else None
    usable = set()
    if profile:
        usable = {fact["text"].strip().casefold() for fact in profile["facts"]
                  if fact["kind"] == "skill" and fact["verified"] and fact["user_approved"]
                  and fact["source"].strip() and fact["confidence"] >= 80}
    timestamp = now()
    imported = 0
    with store.engine.begin() as connection:
        check()
        for item in items:
            check()
            title = plain_text(item.get("title"))[:500]
            application_url = _application_url(item, strategy, endpoint)
            if not title or application_url is None:
                continue
            description_value = (item.get("content_text") or item.get("content_html") or item.get("description")
                                 or item.get("summary") or "")
            description = plain_text(description_value)[:50_000]
            location = plain_text(item.get("location") or "Unknown")[:1000] or "Unknown"
            location_facts = classify_location(location, description)
            skill_data = extract_skills(title + " " + description)
            title_info = normalize_title(title)
            experience = normalize_experience_level(title)
            education = parse_education_requirement(description)
            requirements = parse_job_requirements(description)
            negative = parse_negative_conditions(description, requirements["clearance"])
            salary = parse_salary(description, "EMPLOYER_PROVIDED")
            published = parse_timestamp(item.get("date_published") or item.get("published") or item.get("pubdate"))
            external_key = str(item.get("id") or item.get("guid") or application_url).strip()
            requisition = external_key if len(external_key) <= 128 else identity(external_key)
            job_id = identity(company_id, provider, tenant, requisition)
            previous = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one_or_none()
            previous = dict(previous or {})
            attributes = {
                "provider": provider, "tenant": tenant, "source_id": source_id,
                "source_endpoint": endpoint, "collection_strategy": strategy,
                "reported_posted_at": published.isoformat() if published else previous.get("reported_posted_at"),
                "first_seen_at": previous.get("first_seen_at") or timestamp,
                "last_seen_at": timestamp, "last_collected_at": timestamp,
                "employment_type": normalize_employment_type(title + " " + description),
                "experience_level": experience["level"], "experience_level_raw": experience["raw"],
                "normalized_title": title_info["normalized_title"], "title_family": title_info["title_family"],
                "skill_ids": skill_data["ids"], "skill_mentions": skill_data["mentions"],
                "skill_evidence": "description_mentions", "education_requirement": education["classification"],
                "education_requirement_raw": education["raw"],
                "experience_requirements": requirements["experience"],
                "certifications": requirements["certifications"], "clearance": requirements["clearance"],
                "work_authorization": requirements["work_authorization"], "negative_conditions": negative,
                "salary_min": salary["salary_min"], "salary_max": salary["salary_max"],
                "salary_period": salary["salary_period"], "salary_currency": salary["currency"],
                "salary_source": salary["salary_source"], "salary_raw": salary["salary_raw"],
                "remote_regions": location_facts["remote_regions"],
                "remote_indicated": location_facts["remote_indicated"],
                "country": location_facts["country"], "state": location_facts["state"],
                "city": location_facts["city"], "application_url_status": "PROVIDER_SUPPLIED",
                "submission_capability": "MANUAL_ONLY", "fictional": False,
            }
            job = {"id": job_id, "company_id": company_id, "requisition": requisition,
                   "title": title, "description": description, "skills": skill_data["labels"],
                   "location": location, "remote_type": location_facts["remote_type"],
                   "attributes": attributes}
            upsert(connection, m.jobs, job, replace=True)
            upsert(connection, m.routes, {"id": job_id, "job_id": job_id, "company_id": company_id,
                "provider": provider, "tenant": tenant, "requisition": requisition,
                "url": application_url}, replace=True)
            source = "company-feed:" + str(source_id)
            source_job_id = identity(source, external_key)
            posting_id = identity(source, source_job_id)
            persist_posting(connection, {"id": posting_id, "job_id": job_id,
                "source": source, "source_job_id": source_job_id, "source_url": endpoint,
                "application_url": application_url, "raw": item, "fetched_at": timestamp})
            if profile:
                mentions = {skill.casefold() for skill in job["skills"]}
                score = round(100 * len(mentions & usable) / len(mentions), 2) if mentions else 0
                upsert(connection, m.matches, {"id": identity(job_id, profile_id, profile["version"]),
                    "job_id": job_id, "profile_id": profile_id, "profile_version": profile["version"],
                    "score": score, "missing": sorted(mentions - usable),
                    "components": {"method": "description_skill_mentions", "matched": sorted(mentions & usable),
                        "detected": sorted(mentions),
                        "limitation": "Skill mentions are not verified mandatory requirements or a full qualification assessment."}},
                    replace=True)
            imported += 1
        store._audit(connection, company_id, "COMPANY_FEED_IMPORT_COMPLETE", {
            "company_source_id": source_id, "provider": provider, "strategy": strategy,
            "jobs_enumerated": len(items), "jobs_imported": imported, "profile_id": profile_id,
        })
    return imported
