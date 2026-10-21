"""Bounded local mock discovery flywheel and strong route deduplication."""
from urllib.parse import urljoin, urlsplit
import os
import threading

from bs4 import BeautifulSoup
import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.discovery.sources.collect import persist_posting
from services.discovery.sources.http_transport import SourceTransport
from services.discovery.search_semantics import (
    classify_location, normalize_employment_type, normalize_experience_level, normalize_title,
    parse_education_requirement,
    normalize_reported_timestamp,
)
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skills, skill_label
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.negative_filters import parse_negative_conditions
from services.company_discovery.sources import register_ats_tenant, upsert_company_job_source
from services.company_discovery.flywheel import directly_monitored, discover_company_source
from services.company_discovery.resolution import resolve_company
from services.company_discovery.domains import record_company_domain
from services.discovery.lifecycle import capture_revision, observe_fixture_posting, resolve_lifecycle
from services.discovery.matching import build_explanation
from services.audit_history import record_job_discovered, record_match_scored

MAX_MOCK_RESPONSE_BYTES = 2_000_000
CANCELLATION_POLL_INTERVAL = 0.05


class MockClient:
    # Company discovery requires a transport that checks every redirect against
    # its allowlist. This implementation is restricted to one local fixture origin.
    bounded_redirects = True
    fixture_only = True

    def __init__(self, origin, check=None):
        parsed = urlsplit(origin)
        allowed_hosts = ("127.0.0.1", "localhost") + (("mock",) if os.environ.get("MJOBS_COMPOSE_MOCK") == "1" else ())
        if (parsed.scheme != "http" or parsed.hostname not in allowed_hosts
                or parsed.username or parsed.password or parsed.path not in ("", "/")):
            raise ValueError("Mock origin must be an explicit local fixture server")
        self.origin = origin.rstrip("/")
        self.check = check or (lambda: None)
        # Own connect/read sockets before a response exists so the worker check
        # also bounds DNS, pending connects, and response headers.
        self.client = httpx.Client(transport=SourceTransport(self.check), timeout=10,
                                   follow_redirects=False, trust_env=False)

    def allowed(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.netloc) == (urlsplit(self.origin).scheme, urlsplit(self.origin).netloc) and not parsed.username and not parsed.password

    def get(self, url):
        for _ in range(4):
            self.check()
            if not self.allowed(url):
                raise ValueError("Untrusted external redirect blocked")
            stop_watcher = threading.Event()
            cancellation_errors = []
            active_response = []

            def interrupt_request():
                if active_response:
                    try:
                        active_response[0].close()
                    except Exception:
                        pass
                try:
                    self.client.close()
                except Exception:
                    pass

            def watch_cancellation():
                while not stop_watcher.wait(CANCELLATION_POLL_INTERVAL):
                    try:
                        self.check()
                    except BaseException as error:
                        cancellation_errors.append(error)
                        interrupt_request()
                        return

            watcher = threading.Thread(target=watch_cancellation,
                                       name="mjobs-fixture-discovery-cancellation", daemon=True)
            watcher.start()
            redirect = None
            try:
                with self.client.stream("GET", url) as response:
                    active_response.append(response)
                    if response.is_redirect:
                        redirect = urljoin(url, response.headers["location"])
                    else:
                        response.raise_for_status()
                        content = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            self.check()
                            content.extend(chunk)
                            if len(content) > MAX_MOCK_RESPONSE_BYTES:
                                raise ValueError("Source response exceeds limit")
                        self.check()
                        if cancellation_errors:
                            raise cancellation_errors[0]
                        result = httpx.Response(response.status_code, headers=response.headers,
                                                content=bytes(content), request=response.request)
            except BaseException:
                if cancellation_errors:
                    raise cancellation_errors[0]
                raise
            finally:
                stop_watcher.set()
                if watcher.is_alive():
                    watcher.join(timeout=1)
            if cancellation_errors:
                raise cancellation_errors[0]
            self.check()
            if redirect is not None:
                url = redirect
                continue
            return result
        raise ValueError("Too many source redirects")

    def close(self):
        self.client.close()


def mock_skill_evidence(row, title, description):
    """Separate explicit requirement fields from unclassified posting mentions."""
    mentions = extract_skills(title + " " + description + " " + " ".join(row.get("skills") or []))
    classified = {}
    for field in ("required_skills", "preferred_skills", "nice_to_have_skills"):
        values = row.get(field) or []
        if field == "required_skills":
            # In this controlled mock contract, `skills` is explicitly the
            # structured required-skills field, unlike real-source mentions.
            structured_skills = row.get("skills") or []
            structured_skills = (structured_skills if isinstance(structured_skills, (list, tuple, set))
                                 else [structured_skills])
            values = [*(values if isinstance(values, (list, tuple, set)) else [values]), *structured_skills]
        values = values if isinstance(values, (list, tuple, set)) else [values]
        labels = []
        for value in values:
            identifier = canonical_skill_id(value)
            found = [skill_label(identifier)] if identifier else extract_skills(str(value))["labels"]
            labels.extend(label for label in found if label not in labels)
        classified[field] = labels
    has_structured_requirements = any(classified.values())
    for field in ("required_skills", "preferred_skills", "nice_to_have_skills"):
        for label in classified[field]:
            identifier = canonical_skill_id(label)
            if label not in mentions["labels"]:
                mentions["labels"].append(label)
                mentions["ids"].append(identifier)
    return mentions, {
        "skill_evidence": "structured_requirements" if has_structured_requirements else "description_mentions",
        **classified,
    }


def discover(store, mock_origin, profile_id, check=None, profile_version=None, *, capture_only=False):
    profile = store.profile(profile_id, profile_version)
    client = MockClient(mock_origin, check=check)
    try:
        aggregator = client.get(mock_origin + "/board/jobs").json()["jobs"]
        sources = {}
        all_rows = [("mock-board", row) for row in aggregator]
        for row in aggregator:
            company_website = row.get("company_website") or ""
            company_name = row.get("company") or ""
            candidate = {
                "company_name": company_name,
                "company_website": company_website,
                "application_url": row.get("application_url") or "",
                "trigger_evidence": ["DISCOVERED_VIA_AGGREGATOR_BOARD"],
            }
            if row.get("company_domain"):
                candidate["company_domain"] = row["company_domain"]
            resolved = resolve_company(store, candidate)
            company_id = resolved["company_id"]
            if not directly_monitored(store, company_id):
                discovery_result = discover_company_source(
                    store,
                    candidate,
                    client,
                    check=check,
                    profile_id=profile_id,
                    profile_version=profile["version"] if profile else None,
                )
                endpoint = discovery_result["endpoint"]
                employer_jobs = discovery_result.get("jobs") or []
                sources[company_id] = {
                    "company": company_name, "domain": company_website,
                    "tenant": discovery_result["tenant"], "endpoint": endpoint,
                    "career_url": discovery_result["careers_url"],
                }
            else:
                with store.engine.connect() as connection:
                    source_row = connection.execute(
                        select(m.company_sources).where(
                            m.company_sources.c.company_id == company_id,
                            m.company_sources.c.enabled.is_(True),
                        ).order_by(m.company_sources.c.id)
                    ).mappings().first()
                if source_row:
                    endpoint = source_row["endpoint"]
                    payload = client.get(endpoint).json()
                    employer_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
                    sources[company_id] = {
                        "company": company_name, "domain": company_website,
                        "tenant": source_row["tenant"], "endpoint": endpoint,
                        "career_url": source_row["career_url"],
                    }
                else:
                    endpoint = None
                    employer_jobs = []
            if row.get("company_domain"):
                record_company_domain(
                    store, company_id, row["company_domain"],
                    "FIRST_PARTY_JOB_LINK", ["fixture company domain"],
                )
            if endpoint and not any(source == endpoint for source, _ in all_rows):
                all_rows.extend((endpoint, employer) for employer in employer_jobs)
        captured_job_ids = set()
        with store.engine.begin() as connection:
            client.check()
            for company_id, source in sources.items():
                upsert(connection, m.companies, {"id": company_id, "name": source["company"], "domain": source["domain"]})
            for source, row in all_rows:
                company_id = identity(row["company_website"])
                if not client.allowed(row["application_url"]):
                    raise ValueError("External mock application URL blocked")
                job_id = identity(company_id, "mock-ats", row["tenant"], row["requisition"])
                captured_job_ids.add(job_id)
                description = BeautifulSoup(row["description"], "html.parser").get_text(" ", strip=True)
                location_facts = classify_location(row["location"], description)
                if str(row.get("remote_type") or "").upper() == "REMOTE" and not location_facts["remote_indicated"]:
                    location_facts["remote_indicated"] = True
                    location_facts["remote_type"] = "REMOTE_US" if location_facts["country"] == "US" else "UNKNOWN"
                    if location_facts["country"]:
                        location_facts["remote_regions"] = [location_facts["country"]]
                existing_attributes = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one_or_none()
                existing_attributes = dict(existing_attributes or {})
                experience = normalize_experience_level(row["title"])
                title_info = normalize_title(row["title"])
                education = parse_education_requirement(description)
                requirements = parse_job_requirements(description)
                negative_conditions = parse_negative_conditions(description, requirements["clearance"])
                skill_data, skill_requirements = mock_skill_evidence(row, row["title"], description)
                salary_period = str(row.get("salary_period") or "UNKNOWN").upper()
                if salary_period not in {"HOUR", "YEAR"}:
                    salary_period = "UNKNOWN"
                job = {"id": job_id, "company_id": company_id, "requisition": row["requisition"], "title": row["title"].strip(),
                       "description": description, "skills": skill_data["labels"], "location": row["location"],
                       "remote_type": location_facts["remote_type"],
                       "attributes": {"fictional": True, **skill_requirements, "provider": "mock-ats",
                           "employment_type": normalize_employment_type(row["title"] + " " + description),
                           "experience_level": experience["level"], "experience_level_raw": experience["raw"],
                           "normalized_title": title_info["normalized_title"], "title_family": title_info["title_family"],
                           "skill_ids": skill_data["ids"], "skill_mentions": skill_data["mentions"],
                           "education_requirement": education["classification"], "education_requirement_raw": education["raw"],
                           "experience_requirements": requirements["experience"],
                           "certifications": requirements["certifications"],
                           "clearance": requirements["clearance"],
                           "work_authorization": requirements["work_authorization"],
                           "negative_conditions": negative_conditions,
                           "salary_min": row.get("salary_min"), "salary_max": row.get("salary_max"),
                           "salary_period": salary_period, "salary_currency": row.get("salary_currency"),
                           "salary_source": "SOURCE_PROVIDED" if row.get("salary_min") is not None or row.get("salary_max") is not None else "UNKNOWN",
                           "salary_raw": {key: row.get(key) for key in ("salary_min", "salary_max", "salary_currency", "salary_period")},
                           **normalize_reported_timestamp(row.get('reported_posted_at'),
                               reference_at=now(),
                               previous=existing_attributes),
                           "first_seen_at": existing_attributes.get("first_seen_at") or now(), "last_seen_at": now(),
                           "remote_regions": location_facts["remote_regions"], "remote_indicated": location_facts["remote_indicated"],
                           "country": location_facts["country"], "state": location_facts["state"], "city": location_facts["city"]}}
                previous_job = connection.execute(select(m.jobs).where(m.jobs.c.id == job_id).with_for_update()).mappings().one_or_none()
                if previous_job:
                    capture_revision(connection, previous_job, recorded_at=now(),
                                     observed_at=previous_job['attributes'].get('last_seen_at'))
                upsert(connection, m.jobs, job, replace=source != "mock-board")
                persisted = connection.execute(select(m.jobs).where(m.jobs.c.id == job_id)).mappings().one()
                capture_revision(connection, persisted, recorded_at=now(), observed_at=persisted['attributes'].get('last_seen_at'))
                if source == "mock-board":
                    board_route_id = "z" + identity("route", "mock-board", job_id)[1:]
                    upsert(connection, m.routes, {
                        "id": board_route_id, "job_id": job_id, "company_id": company_id,
                        "provider": "mock-board", "tenant": "board",
                        "requisition": row["requisition"], "url": row["application_url"],
                    }, replace=True)
                else:
                    upsert(connection, m.routes, {
                        "id": job_id, "job_id": job_id, "company_id": company_id,
                        "provider": "mock-ats", "tenant": row["tenant"],
                        "requisition": row["requisition"], "url": row["application_url"],
                    }, replace=True)
                persist_posting(connection, {"id": identity(source, row["source_job_id"]), "job_id": job_id,
                    "source": source, "source_job_id": row["source_job_id"], "source_url": source,
                    "application_url": row["application_url"], "raw": row, "fetched_at": now()})
                observe_fixture_posting(connection, identity(source, row['source_job_id']))
                resolve_lifecycle(connection, job_id)
                if not capture_only:
                    explanation = build_explanation(job, profile)
                    had_profile_match = connection.execute(select(m.matches.c.id).where(
                        m.matches.c.job_id == job_id,
                        m.matches.c.profile_id == profile_id,
                    ).limit(1)).first() is not None
                    if not had_profile_match:
                        record_job_discovered(connection, profile_id, job_id)
                    record_match_scored(connection, profile_id, job_id, profile["version"])
                    upsert(connection, m.matches, {"id": identity(job_id, profile_id, profile["version"]), "job_id": job_id,
                        "profile_id": profile_id, "profile_version": profile["version"], **explanation}, replace=True)
            store._audit(connection, profile_id, "MOCK_DISCOVERY_COMPLETE", {"sources": len(sources), "raw_postings": len(all_rows)})
        if capture_only:
            return {"job_ids": sorted(captured_job_ids), "postings_captured": len(all_rows)}
        return store.jobs(profile_id)
    finally:
        client.close()
