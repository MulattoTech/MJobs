"""Bind advisory evaluations to the public job facts actually reviewed."""
import hashlib
import json

from sqlalchemy import select

from packages.persistence import models as m


def job_fingerprint(job):
    # Polling timestamps and source-selection bookkeeping are not job facts.
    # Retain full posting text (including text beyond the provider prompt cap)
    # and all remaining structured attributes used by matching/preparation.
    attributes = dict(job.get('attributes') or {})
    for key in ('first_seen_at', 'last_seen_at', 'source_resolution', 'lifecycle'):
        attributes.pop(key, None)
    snapshot = {key: job.get(key) for key in (
        'id', 'company_id', 'company', 'title', 'description', 'skills', 'location', 'remote_type')}
    snapshot['attributes'] = attributes
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def current_fingerprints(connection, job_ids, *, lock=False):
    if not job_ids:
        return {}
    query = select(m.jobs, m.companies.c.name.label('company')).join(
        m.companies, m.jobs.c.company_id == m.companies.c.id).where(
        m.jobs.c.id.in_(job_ids)).order_by(m.jobs.c.id)
    if lock:
        query = query.with_for_update(of=m.jobs)
    return {row['id']: job_fingerprint(row) for row in connection.execute(query).mappings()}


def require_current(connection, fingerprints):
    if not isinstance(fingerprints, dict) or any(
            not isinstance(value, str) or len(value) != 64 for value in fingerprints.values()):
        raise ValueError('AI_EVALUATION_INPUT_UNKNOWN')
    current = current_fingerprints(connection, list(fingerprints), lock=True)
    if current != fingerprints:
        raise ValueError('AI_EVALUATION_JOB_CHANGED')
