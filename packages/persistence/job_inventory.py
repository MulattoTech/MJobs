"""Bounded, content-addressed enumeration of the whole persisted job catalogue.

Scope is workspace-global, bound to an existing profile/version for recovery;
this is not a matching/search view or an authorization boundary. It uses the
same local-workspace API authority as unfiltered GET /jobs. No tracking filter
or route join can remove a persisted job. Current canonical columns, company
identity, every route, posting identity/raw payload and immutable posting
snapshots contribute to the digest. Audit/quality observation history remains
in its separate persistence domain.

Only polling metadata is excluded: posting fetched_at, snapshot retrieved_at,
canonical last_seen_at/last_collected_at, lifecycle observation ID/time, and
source-resolution evaluation/candidate observation IDs/time/count. Resolution
candidate semantic values are deduplicated; authority, quality, conflicts,
selected posting and route decisions remain significant. Provider raw payloads
are NEVER projected or normalized.
"""
import base64
from collections import defaultdict
import hashlib
import json
import re

from sqlalchemy import select
from packages.persistence import models as m

SCOPE = 'ALL_PERSISTED_JOBS'
MAX_JOBS = 10_000
PAGE_SIZE = 200
MAX_COPIES = 100_000
MAX_PER_JOB = 1_000
MAX_SNAPSHOTS = 100_000
MAX_BYTES = 128 * 1024 * 1024
HEX = re.compile(r'[a-f0-9]{64}\Z')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def stable_attributes(value):
    if not isinstance(value, dict):
        raise ValueError('JOB_INVENTORY_INVALID_PERSISTED_DATA')
    attrs = dict(value)
    for name in ('last_seen_at', 'last_collected_at'):
        attrs.pop(name, None)
    if isinstance(attrs.get('lifecycle'), dict):
        attrs['lifecycle'] = {key: item for key, item in attrs['lifecycle'].items()
                              if key not in {'observation_id', 'observed_at'}}
    if isinstance(attrs.get('source_resolution'), dict):
        resolution = dict(attrs['source_resolution'])
        for name in ('evaluated_at', 'selected_candidate_id', 'candidate_count'):
            resolution.pop(name, None)
        if isinstance(resolution.get('candidates'), list):
            if any(not isinstance(row, dict) for row in resolution['candidates']):
                raise ValueError('JOB_INVENTORY_INVALID_PERSISTED_DATA')
            candidates = [{key: item for key, item in row.items() if key not in {'id', 'observed_at'}}
                          for row in resolution['candidates']]
            resolution['candidates'] = [json.loads(value) for value in sorted({encoded(row) for row in candidates})]
        attrs['source_resolution'] = resolution
    return attrs


def snapshot_digest(profile_id, profile_version, rows):
    return digest(dict(scope=SCOPE, profile_id=profile_id, profile_version=profile_version, jobs=rows))


def _fail():
    raise ValueError('JOB_INVENTORY_BUDGET_EXCEEDED')


def _manifest(connection, profile_id):
    profile = connection.execute(select(m.profiles.c.version).where(m.profiles.c.id == profile_id)).scalar_one_or_none()
    if profile is None:
        raise LookupError('JOB_INVENTORY_PROFILE_NOT_FOUND')
    byte_count = 0

    def bounded(statement, cap):
        nonlocal byte_count
        with connection.execute(statement.limit(cap + 1)) as result:
            for index, mapping in enumerate(result.mappings()):
                if index >= cap:
                    _fail()
                row = dict(mapping)
                byte_count += len(encoded(row))
                if byte_count > MAX_BYTES:
                    _fail()
                yield row

    jobs = list(bounded(select(m.jobs).order_by(m.jobs.c.id), MAX_JOBS))
    companies = {row['id']: row for row in bounded(select(m.companies).where(
        m.companies.c.id.in_(select(m.jobs.c.company_id))), MAX_JOBS)}
    routes, copies, snapshots = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in bounded(select(m.routes).order_by(m.routes.c.id), MAX_COPIES):
        routes[row['job_id']].append(row)
        if len(routes[row['job_id']]) > MAX_PER_JOB:
            _fail()
    for row in bounded(select(m.posting_snapshots).order_by(m.posting_snapshots.c.id), MAX_SNAPSHOTS):
        snapshots[row['posting_id']].append(dict(id=row['id'], sha256=row['sha256'], raw_sha256=digest(row['raw'])))
        if len(snapshots[row['posting_id']]) > MAX_PER_JOB:
            _fail()
    for row in bounded(select(m.postings).order_by(m.postings.c.id), MAX_COPIES):
        row.pop('fetched_at')
        row['snapshots'] = sorted(snapshots[row['id']], key=lambda item: item['id'])
        copies[row['job_id']].append(dict(posting_id=row['id'], source=row['source'],
            source_job_id=row['source_job_id'], source_url=row['source_url'], sha256=digest(row)))
        if len(copies[row['job_id']]) > MAX_PER_JOB:
            _fail()
    rows = []
    for job in jobs:
        job['attributes'] = stable_attributes(job['attributes'])
        job_id = job['id']
        copies[job_id].sort(key=lambda item: item['posting_id'])
        routes[job_id].sort(key=lambda item: item['id'])
        rows.append(dict(job_id=job_id, canonical_sha256=digest(dict(
            job=job, company=companies[job['company_id']], routes=routes[job_id])),
            route_count=len(routes[job_id]), source_copy_count=len(copies[job_id]),
            source_copies=copies[job_id], source_copies_sha256=digest(copies[job_id])))
    # Python ordering is identical for both backends, independent of DB collation.
    return profile, sorted(rows, key=lambda row: row['job_id'])


def inventory_page(store, profile_id, *, limit=PAGE_SIZE, cursor=None, snapshot_sha256=None):
    if (type(limit) is not int or not 1 <= limit <= PAGE_SIZE or not isinstance(profile_id, str)
            or not profile_id or len(profile_id) > 128):
        raise ValueError('JOB_INVENTORY_INVALID_REQUEST')
    after = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise ValueError()
            value = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            if (set(value) != {'profile_id', 'snapshot_sha256', 'after'} or value['profile_id'] != profile_id
                    or not isinstance(value['after'], str) or not value['after'] or len(value['after']) > 128
                    or not isinstance(value['snapshot_sha256'], str) or not HEX.fullmatch(value['snapshot_sha256'])
                    or (snapshot_sha256 is not None and snapshot_sha256 != value['snapshot_sha256'])):
                raise ValueError()
            after, snapshot_sha256 = value['after'], value['snapshot_sha256']
        except (ValueError, TypeError, KeyError):
            raise ValueError('JOB_INVENTORY_INVALID_CURSOR') from None
    if snapshot_sha256 is not None and (not isinstance(snapshot_sha256, str) or not HEX.fullmatch(snapshot_sha256)):
        raise ValueError('JOB_INVENTORY_INVALID_SNAPSHOT')
    with store.read_snapshot() as connection:
        profile_version, rows = _manifest(connection, profile_id)
    snapshot = snapshot_digest(profile_id, profile_version, rows)
    if snapshot_sha256 is not None and snapshot != snapshot_sha256:
        raise ValueError('JOB_INVENTORY_SNAPSHOT_CHANGED')
    if after is not None and after not in {row['job_id'] for row in rows}:
        raise ValueError('JOB_INVENTORY_INVALID_CURSOR')
    remaining = [row for row in rows if after is None or row['job_id'] > after]
    selected = remaining[:limit]
    complete = len(remaining) <= limit
    next_cursor = None if complete else base64.urlsafe_b64encode(encoded(dict(
        profile_id=profile_id, snapshot_sha256=snapshot, after=selected[-1]['job_id']))).decode('ascii')
    return dict(schema_version=4, scope=SCOPE, profile_id=profile_id, profile_version=profile_version,
        total=len(rows), source_copy_count=sum(row['source_copy_count'] for row in rows),
        snapshot_sha256=snapshot, after=after, jobs=selected, complete=complete, next_cursor=next_cursor)
