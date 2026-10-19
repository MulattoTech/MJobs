"""Acquire employer authority from reciprocal native ATS and employer observations.

A configured native API's custom job URL authorizes a bounded candidate read,
not domain verification. Employer markup must independently identify that exact
job and link back to its exact native tenant/requisition before promotion.
"""
import hashlib
import json
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, now, upsert
from services.ats_detection.board_links import greenhouse_board_tenants, lever_board_tenants
from services.company_discovery.application_links import _identifier, _jsonld_postings, _title
from services.company_discovery.domains import lock_domain_evidence, normalize_domain
from services.company_discovery.fetch_policy import bounded_get
from services.company_discovery.public_client import PublicFetchError


CANDIDATE = 'ATS_JOB_LINK_CANDIDATE'
VERIFIED = 'ATS_JOB_LINK_VERIFIED'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def native_job_link(connection, request):
    """Return a source-bound candidate; raw provider labels grant no authority."""
    from services.company_discovery.requests import _website

    source = connection.execute(select(m.company_sources).where(
        m.company_sources.c.id == request['company_source_id'])).mappings().one()
    if source['provider'] != 'greenhouse' or not source['enabled'] or not source['collector_source_id']:
        return None
    collector = connection.execute(select(m.job_sources).where(
        m.job_sources.c.id == source['collector_source_id'])).mappings().one()
    tenant = source['tenant']
    endpoint = 'https://boards-api.greenhouse.io/v1/boards/' + tenant + '/jobs'
    if (not collector['enabled'] or collector['provider'] != 'greenhouse'
            or collector['tenant'] != tenant or source['endpoint'] != endpoint
            or source['company_id'] != identity('greenhouse', tenant)):
        return None
    candidate = connection.execute(select(m.posting_value_candidates).where(
        m.posting_value_candidates.c.id == request['candidate_id'])).mappings().one()
    snapshot = connection.execute(select(m.posting_snapshots).where(
        m.posting_snapshots.c.id == candidate['posting_snapshot_id'])).mappings().one()
    posting = connection.execute(select(m.postings).where(
        m.postings.c.id == candidate['posting_id'])).mappings().one()
    observation = connection.execute(select(m.source_quality_observations).where(
        m.source_quality_observations.c.id == candidate['quality_observation_id'])).mappings().one()
    raw = snapshot['raw']
    attrs = candidate['normalized']['attributes']
    if (not isinstance(raw, dict) or snapshot['posting_id'] != posting['id']
            or candidate['company_source_id'] != source['id']
            or posting['job_id'] != request['trigger_job_id']
            or snapshot['sha256'] != _digest(raw) or _digest(posting['raw']) != snapshot['sha256']
            or not observation['succeeded'] or not observation['available']
            or observation['configured_source_id'] != collector['id']
            or observation['source_endpoint'] != endpoint
            or attrs.get('provider') != 'greenhouse' or attrs.get('tenant') != tenant
            or attrs.get('source_endpoint') != endpoint
            or type(raw.get('id')) is not int or raw['id'] <= 0
            or attrs.get('native_posting_id') != raw['id']
            or candidate['normalized']['requisition'] != str(raw['id'])):
        return None
    try:
        website = _website(raw.get('absolute_url'))
    except (ValueError, PublicFetchError):
        return None
    if not normalize_domain(website):
        return None
    domain = normalize_domain(website)
    existing = connection.execute(select(m.company_domains).where(
        (m.company_domains.c.domain == domain) | (
            (m.company_domains.c.company_id == request['company_id']) &
            m.company_domains.c.verified.is_(True)))).mappings()
    if any(row['company_id'] != request['company_id'] or row['domain'] != domain for row in existing):
        return None
    if request['profile_id']:
        profile = Store._profile_on(connection, request['profile_id'])
        policy = (profile.get('preferences') or {}).get('company_discovery') or {}
        if profile['version'] != request['profile_version'] or policy.get('company_discovery_enabled', True) is not True:
            return None
    return dict(website=website, source_id=source['id'], collector_source_id=collector['id'],
        provider='greenhouse', tenant=tenant, endpoint=endpoint, candidate_id=candidate['id'],
        snapshot_id=snapshot['id'], snapshot_sha256=snapshot['sha256'],
        quality_observation_id=observation['id'],
        native_job_id=str(raw['id']), title=candidate['normalized']['title'],
        profile_id=request['profile_id'], profile_version=request['profile_version'])


def _same_origin(first, second):
    def origin(value):
        try:
            url = urlsplit(value)
            if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
                return None
            return url.scheme, url.hostname
        except (TypeError, ValueError):
            return None
    return origin(first) is not None and origin(first) == origin(second)


def corroborated_job_page(proof, company_name, response):
    """Require independent reciprocal infrastructure, not JSON-LD identity alone."""
    final_url = str(response.url)
    if response.status_code != 200 or not _same_origin(proof['website'], final_url):
        return False
    nodes = _jsonld_postings(response.text)
    if nodes is None or len(nodes) != 1:
        return False
    node = nodes[0]
    organization = node.get('hiringOrganization')
    if (not isinstance(organization, dict) or _title(organization.get('name')) != _title(company_name)
            or not _same_origin(organization.get('url'), final_url)
            or _identifier(node) != proof['native_job_id']
            or _title(node.get('title')) != _title(proof['title'])
            or node.get('url') not in {proof['website'], final_url}):
        return False
    try:
        if greenhouse_board_tenants(final_url, response.text) != {proof['tenant']} or lever_board_tenants(final_url, response.text):
            return False
    except ValueError:
        return False
    expected_paths = {'/' + proof['tenant'] + '/jobs/' + proof['native_job_id']}
    for link in BeautifulSoup(response.text, 'html.parser').find_all('a', href=True):
        try:
            url = urlsplit(link['href'])
            if (url.scheme == 'https' and url.hostname in {'boards.greenhouse.io', 'job-boards.greenhouse.io'}
                    and not url.username and not url.password and url.port in (None, 443)
                    and not url.query and not url.fragment and url.path in expected_paths):
                return True
        except ValueError:
            pass
    return False


def acquire_official_domain(store, authorization, client, check, enrollment_guard):
    """Corroborate outside transactions, then atomically authorize live evidence."""
    from services.company_discovery.requests import _origin

    proof = authorization['native_job_link']
    check()
    response = bounded_get(client, proof['website'])
    check()
    with store.engine.connect() as connection:
        company_name = connection.execute(select(m.companies.c.name).where(
            m.companies.c.id == authorization['company_id'])).scalar_one()
    if not corroborated_job_page(proof, company_name, response):
        raise PublicFetchError('COMPANY_DOMAIN_CORROBORATION_REQUIRED')
    if enrollment_guard is None:
        raise PublicFetchError('DISCOVERY_AUTHORIZATION_REQUIRED')
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        from services.dedupe.ingestion_identity import lock_company_ingestion
        lock_company_ingestion(connection, [authorization['company_id']])
        domain = normalize_domain(proof['website'])
        rows = lock_domain_evidence(connection, domain)
        if any(row['company_id'] != authorization['company_id'] for row in rows):
            raise PublicFetchError('COMPANY_IDENTITY_REVIEW_REQUIRED')
        other_domains = connection.execute(select(m.company_domains.c.domain).where(
            m.company_domains.c.company_id == authorization['company_id'],
            m.company_domains.c.verified.is_(True), m.company_domains.c.domain != domain)).scalars().all()
        if other_domains:
            raise PublicFetchError('COMPANY_IDENTITY_REVIEW_REQUIRED')
        current = enrollment_guard(connection)
        if current != authorization:
            raise PublicFetchError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
        record_id = identity(authorization['company_id'], domain)
        evidence = ['Native ATS job URL reciprocally corroborated by employer job page.',
            json.dumps(dict(request_id=authorization['request_id'], **proof), sort_keys=True),
            'Employer response SHA256: ' + hashlib.sha256(response.content).hexdigest()]
        old = next((row for row in rows if row['id'] == record_id), None)
        if not old or not old['verified']:
            upsert(connection, m.company_domains, dict(id=record_id, company_id=authorization['company_id'],
                domain=domain, confidence=0.99, verified=True, verification_method='FIRST_PARTY_JOB_LINK',
                first_seen_at=old['first_seen_at'] if old else now(), last_verified_at=now(), evidence=evidence), replace=True)
        _, domain_proof = _origin(connection, authorization['company_id'])
        if len(domain_proof) != 1 or domain_proof[0]['domain_id'] != record_id:
            raise PublicFetchError('COMPANY_IDENTITY_REVIEW_REQUIRED')
        connection.execute(update(m.companies).where(
            m.companies.c.id == authorization['company_id']).values(domain=domain))
        connection.execute(update(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == authorization['request_id']).values(
                authorization=VERIFIED, domain_evidence=[proof, *domain_proof]))
        store._audit(connection, authorization['company_id'], 'COMPANY_DOMAIN_ACQUIRED', dict(
            request_id=authorization['request_id'], method='FIRST_PARTY_JOB_LINK',
            snapshot_sha256=proof['snapshot_sha256'], response_sha256=hashlib.sha256(response.content).hexdigest()))
    return response
