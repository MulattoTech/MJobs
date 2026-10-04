"""Conservative attachment of new source postings; never merge existing jobs."""
import hashlib
from urllib.parse import urlsplit

from sqlalchemy import select, text

from packages.persistence import models as m
from services.dedupe.signals import canonical_application_url


def lock_company_ingestion(connection, company_ids):
    """Serialize identity selection before company/source/job locks on PostgreSQL.

    SQLite ingestion already holds BEGIN IMMEDIATE. Transaction-scoped advisory
    locks also cover companies that have not yet been inserted by an adapter.
    """
    if connection.dialect.name == 'postgresql':
        for company_id in sorted(set(company_ids)):
            key = int.from_bytes(hashlib.sha256(
                ('mjobs-ingestion-company:' + company_id).encode()).digest()[:8], 'big', signed=True)
            connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


def specific_application_url(value):
    """Exclude landing/search URLs from automatic exact-route identity evidence."""
    try:
        parsed = urlsplit(str(value or ''))
        if parsed.username or parsed.password:
            return ''
        canonical = canonical_application_url(value)
        parts = [part.casefold() for part in urlsplit(canonical).path.split('/') if part]
    except ValueError:
        return ''
    if len(parts) < 2 or parts[-1] in {
        'jobs', 'careers', 'career', 'apply', 'search', 'openings', 'positions',
        'job-search', 'jobsearch', 'index.html', 'index.php',
    }:
        return ''
    return canonical


def canonical_ingestion_job(connection, *, company_id, proposed_id, posting_id, application_url,
                            provider, tenant, requisition, batch_identities):
    """Return the existing job to retain, or None to create the native identity.

    Callers hold the company ingestion lock. Posting identity and already-created
    native jobs are sticky. Only an unambiguous exact route within the same stored
    company can attach a new posting; historic duplicate jobs remain reviewable.
    Route reads never take locks before the job lock used by application code.
    """
    existing_id = connection.execute(select(m.postings.c.job_id).where(
        m.postings.c.id == posting_id)).scalar_one_or_none()
    existing_id = existing_id or proposed_id
    existing = connection.execute(select(m.jobs).where(
        m.jobs.c.id == existing_id).with_for_update()).mappings().one_or_none()
    if existing is not None:
        if existing['company_id'] != company_id:
            raise ValueError('SOURCE_VALUE_CANONICAL_IDENTITY_MISMATCH')
        return dict(existing)
    url = specific_application_url(application_url)
    if not url:
        return None
    routes = connection.execute(select(m.routes).where(m.routes.c.company_id == company_id)).mappings().all()
    matching = [route for route in routes if specific_application_url(route['url']) == url]
    job_ids = {route['job_id'] for route in matching}
    if len(job_ids) != 1:
        return None
    target = connection.execute(select(m.jobs).where(
        m.jobs.c.id == next(iter(job_ids)), m.jobs.c.company_id == company_id
    ).with_for_update()).mappings().one_or_none()
    if target is None:
        return None
    # Explicitly different native requisitions from one provider/tenant outrank
    # a reused application URL. Check preserved candidates as well as the route
    # because the first arrival may have been an aggregator.
    native_identities = [(route['provider'], route['tenant'], route['requisition']) for route in matching]
    native_identities.extend(batch_identities.get(target['id'], []))
    candidates = connection.execute(select(m.posting_value_candidates.c.normalized).join(
        m.postings, m.postings.c.id == m.posting_value_candidates.c.posting_id
    ).where(m.postings.c.job_id == target['id'])).scalars()
    native_identities.extend((value['attributes'].get('provider'), value['attributes'].get('tenant'),
                              value['requisition']) for value in candidates)
    if any(p == provider and t == tenant and r != requisition for p, t, r in native_identities):
        return None
    # A preparation/refresh may have changed the route while we waited for job.
    current_routes = connection.execute(select(m.routes.c.url).where(
        m.routes.c.id.in_([route['id'] for route in matching]))).scalars()
    return dict(target) if any(specific_application_url(value) == url for value in current_routes) else None
