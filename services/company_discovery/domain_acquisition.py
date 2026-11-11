"""Acquire employer authority from reciprocal native ATS and employer observations.

A configured native API's custom job URL authorizes a bounded candidate read,
not domain verification. Employer markup must independently identify that exact
job and link back to its exact native tenant/requisition before promotion.
"""
import hashlib
import json
from urllib.parse import parse_qsl, urljoin, urlsplit

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


def _canonical_url(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    links = [link for link in soup.find_all('link', href=True)
        if 'canonical' in [str(item).casefold() for item in (link.get('rel') or [])]]
    if len(links) != 1:
        return None
    return str(links[0].get('href') or '')


def _canonical_matches_redirect(proof, canonical, final_url):
    """Accept only the exact Greenhouse gh_jid query removed by a same-path canonical."""
    try:
        requested = urlsplit(proof['website'])
        final = urlsplit(final_url)
        normalized = urlsplit(canonical)
        requested_query = parse_qsl(requested.query, keep_blank_values=True,
            strict_parsing=True, max_num_fields=2)
        final_query = parse_qsl(final.query, keep_blank_values=True,
            strict_parsing=True, max_num_fields=2)
    except (KeyError, TypeError, ValueError):
        return False
    native_id = str(proof.get('native_job_id') or '')
    return (bool(native_id)
        and requested_query == [('gh_jid', native_id)]
        and final_query == [('gh_jid', native_id)]
        and requested.fragment == final.fragment == normalized.fragment == ''
        and _same_origin(proof['website'], final_url)
        and _same_origin(canonical, final_url)
        and not normalized.query
        and normalized.path == final.path
        and normalized.path.rstrip('/') == final.path.rstrip('/')
    )


def _job_page_identity_matches(proof, company_name, response):
    """Check the employer page's single structured posting against its bound ATS job."""
    final_url = str(response.url)
    if response.status_code != 200 or not _same_origin(proof['website'], final_url):
        return False
    nodes = _jsonld_postings(response.text)
    if nodes is None or len(nodes) != 1:
        return False
    node = nodes[0]
    organization = node.get('hiringOrganization')
    # JobPosting markup may identify the employer website with scalar sameAs.
    # An explicit url must still pass; sameAs cannot repair conflicting evidence.
    organization_url = (organization.get('url', organization.get('sameAs'))
        if isinstance(organization, dict) else None)
    job_url = node.get('url')
    canonical = _canonical_url(response)
    url_matches = (isinstance(job_url, str) and (job_url in {proof['website'], final_url}
        or (job_url == canonical and _canonical_matches_redirect(proof, canonical, final_url))))
    if (not isinstance(organization, dict) or _title(organization.get('name')) != _title(company_name)
            or not isinstance(organization_url, str) or not _same_origin(organization_url, final_url)
            or _identifier(node) != proof['native_job_id']
            or _title(node.get('title')) != _title(proof['title'])
            or not url_matches):
        return False
    return True


def _direct_native_job_link(proof, response):
    final_url = str(response.url)
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


def _apply_page_url(proof, response):
    """Return one exact same-origin apply route for this native job, if advertised."""
    final_url = str(response.url)
    native_id = str(proof.get('native_job_id') or '')
    if not native_id:
        return None
    candidates = set()
    try:
        links = BeautifulSoup(response.text, 'html.parser').find_all('a', href=True)
        for link in links:
            target = urljoin(final_url, str(link['href']))
            parsed = urlsplit(target)
            segments = [part for part in parsed.path.rstrip('/').split('/') if part]
            if not segments or segments[-1] != native_id or not any(
                    part.casefold() == 'apply' for part in segments):
                continue
            if (not _same_origin(final_url, target) or parsed.query or parsed.fragment):
                return None
            candidates.add(target)
    except (KeyError, TypeError, ValueError):
        return None
    return next(iter(candidates)) if len(candidates) == 1 else None


def _apply_page_has_native_greenhouse_frame(proof, response, job_page_url):
    if (response is None or response.status_code != 200
            or not _same_origin(job_page_url, str(response.url))
            or lever_board_tenants(str(response.url), response.text)):
        return False
    native_id = str(proof.get('native_job_id') or '')
    tenant = str(proof.get('tenant') or '').casefold()
    frames = []
    for frame in BeautifulSoup(response.text, 'html.parser').find_all('iframe', src=True):
        raw = str(frame.get('src') or '')
        try:
            target = urlsplit(urljoin(str(response.url), raw))
            if (target.scheme.casefold() == 'https'
                    and target.hostname in {'boards.greenhouse.io', 'job-boards.greenhouse.io'}
                    and target.username is None and target.password is None
                    and target.port in (None, 443) and target.path == '/embed/job_app'):
                frames.append(target)
        except (TypeError, ValueError):
            continue
    if len(frames) != 1:
        return False
    frame = frames[0]
    try:
        other_tenants = greenhouse_board_tenants(str(response.url), response.text)
        values = parse_qsl(frame.query, keep_blank_values=True,
            strict_parsing=True, max_num_fields=4)
    except ValueError:
        return False
    return (not other_tenants - {tenant}
        and not frame.fragment and len(values) == 2
        and len({key for key, _ in values}) == 2
        and dict(values) == {'for': tenant, 'token': native_id})


def corroborated_job_page(proof, company_name, response, apply_response=None):
    """Require exact employer JobPosting identity and a reciprocal native ATS reference."""
    if not _job_page_identity_matches(proof, company_name, response):
        return False
    if _direct_native_job_link(proof, response):
        return True
    try:
        if (greenhouse_board_tenants(str(response.url), response.text)
                or lever_board_tenants(str(response.url), response.text)):
            return False
    except ValueError:
        return False
    return _apply_page_has_native_greenhouse_frame(proof, apply_response, str(response.url))


def acquire_official_domain_with_native_proof(store, authorization, client, check, enrollment_guard):
    """Corroborate outside transactions, then atomically authorize live evidence."""
    from services.company_discovery.requests import _origin

    proof = authorization['native_job_link']
    check()
    response = bounded_get(client, proof['website'])
    check()
    with store.engine.connect() as connection:
        company_name = connection.execute(select(m.companies.c.name).where(
            m.companies.c.id == authorization['company_id'])).scalar_one()
    apply_response = None
    if not _job_page_identity_matches(proof, company_name, response):
        raise PublicFetchError('COMPANY_DOMAIN_CORROBORATION_REQUIRED')
    direct_native_link = _direct_native_job_link(proof, response)
    if not direct_native_link:
        try:
            if (greenhouse_board_tenants(str(response.url), response.text)
                    or lever_board_tenants(str(response.url), response.text)):
                raise PublicFetchError('COMPANY_DOMAIN_CORROBORATION_REQUIRED')
        except ValueError as error:
            raise PublicFetchError('COMPANY_DOMAIN_CORROBORATION_REQUIRED') from error
        apply_url = _apply_page_url(proof, response)
        if apply_url is None:
            raise PublicFetchError('COMPANY_DOMAIN_CORROBORATION_REQUIRED')
        check()
        apply_response = bounded_get(client, apply_url, expected_origin=str(response.url))
        check()
    if not corroborated_job_page(proof, company_name, response, apply_response=apply_response):
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
        apply_response_sha256 = None
        if apply_response is not None:
            apply_response_sha256 = hashlib.sha256(apply_response.content).hexdigest()
            evidence.append('Employer apply response SHA256: ' + apply_response_sha256)
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
            snapshot_sha256=proof['snapshot_sha256'], response_sha256=hashlib.sha256(response.content).hexdigest(),
            apply_response_sha256=apply_response_sha256))
    # Carry only the identity and content digests needed by downstream
    # enrollment. Raw employer HTML and the apply page are not persisted or
    # handed to the collector.
    native_apply_proof = dict(
        request_id=authorization['request_id'],
        company_id=authorization['company_id'],
        profile_id=proof['profile_id'],
        profile_version=proof['profile_version'],
        source_id=proof['source_id'],
        collector_source_id=proof['collector_source_id'],
        candidate_id=proof['candidate_id'],
        snapshot_id=proof['snapshot_id'],
        snapshot_sha256=proof['snapshot_sha256'],
        quality_observation_id=proof['quality_observation_id'],
        provider='greenhouse',
        tenant=proof['tenant'],
        native_job_id=proof['native_job_id'],
        title=proof['title'],
        reciprocal_method=('DIRECT_NATIVE_JOB_LINK' if direct_native_link else 'SAME_ORIGIN_APPLY_PAGE'),
        employer_url=str(response.url),
        employer_response_sha256=hashlib.sha256(response.content).hexdigest(),
        apply_url=str(apply_response.url) if apply_response is not None else None,
        apply_response_sha256=apply_response_sha256,
    )
    return response, native_apply_proof


def acquire_official_domain(store, authorization, client, check, enrollment_guard):
    """Compatibility wrapper for callers that need only the fetched page."""
    response, _proof = acquire_official_domain_with_native_proof(
        store, authorization, client, check, enrollment_guard)
    return response


def acquire_official_domain_from_aggregator_bootstrap(store, authorization, check, enrollment_guard):
    """Promote an internally persisted aggregator/native receipt without egress."""
    if (not isinstance(authorization, dict)
            or authorization.get('authorization') != 'AGGREGATOR_BOOTSTRAP_VERIFIED'
            or not isinstance(authorization.get('aggregator_bootstrap_receipt'), dict)
            or not isinstance(authorization.get('aggregator_bootstrap_envelope'), dict)
            or enrollment_guard is None):
        raise PublicFetchError('DISCOVERY_AUTHORIZATION_REQUIRED')
    check()
    from services.company_discovery.requests import _origin, _website
    receipt = authorization['aggregator_bootstrap_receipt']
    envelope = authorization['aggregator_bootstrap_envelope']
    website = _website(receipt.get('employer_url'))
    domain = normalize_domain(website)
    if not domain:
        raise PublicFetchError('COMPANY_DOMAIN_CORROBORATION_REQUIRED')
    request_id = authorization.get('request_id')
    company_id = authorization.get('company_id')
    if not request_id or not company_id:
        raise PublicFetchError('DISCOVERY_AUTHORIZATION_CHANGED')
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        from services.dedupe.ingestion_identity import lock_company_ingestion
        lock_company_ingestion(connection, [company_id])
        rows = lock_domain_evidence(connection, domain)
        if any(row['company_id'] != company_id for row in rows):
            raise PublicFetchError('COMPANY_IDENTITY_REVIEW_REQUIRED')
        other_domains = connection.execute(select(m.company_domains.c.domain).where(
            m.company_domains.c.company_id == company_id,
            m.company_domains.c.verified.is_(True), m.company_domains.c.domain != domain)).scalars().all()
        if other_domains:
            raise PublicFetchError('COMPANY_IDENTITY_REVIEW_REQUIRED')
        current = enrollment_guard(connection)
        if current != authorization:
            raise PublicFetchError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')
        record_id = identity(company_id, domain)
        old = next((row for row in rows if row['id'] == record_id), None)
        proof_record = dict(
            method='AGGREGATOR_NATIVE_BOOTSTRAP_V1',
            request_id=request_id,
            pending_sha256=envelope['pending_sha256'],
            receipt_sha256=receipt['sha256'],
            raw_sha256=receipt['raw_sha256'],
            normalized_sha256=receipt['normalized_sha256'],
            source_snapshot_sha256=receipt['source_snapshot_sha256'],
            source_policy_sha256=receipt['source_policy_sha256'],
            employer_url=receipt['employer_url'],
            job_response_sha256=receipt['job_response_sha256'],
            apply_url=receipt['apply_url'],
            apply_response_sha256=receipt['apply_response_sha256'],
            provider=receipt['native']['provider'],
            tenant=receipt['native']['tenant'],
            native_job_id=receipt['native']['job_id'],
            native_response_sha256=receipt['native']['native_response_sha256'],
        )
        evidence = ['Public aggregator posting identity reciprocally corroborated by an exact native ATS job and employer page.',
            json.dumps(proof_record, sort_keys=True)]
        if not old or not old['verified']:
            upsert(connection, m.company_domains, dict(id=record_id, company_id=company_id,
                domain=domain, confidence=0.99, verified=True, verification_method='FIRST_PARTY_JOB_LINK',
                first_seen_at=old['first_seen_at'] if old else now(),
                last_verified_at=now(), evidence=evidence), replace=True)
        _, domain_proof = _origin(connection, company_id)
        if len(domain_proof) != 1 or domain_proof[0]['domain_id'] != record_id:
            raise PublicFetchError('COMPANY_IDENTITY_REVIEW_REQUIRED')
        connection.execute(update(m.companies).where(
            m.companies.c.id == company_id).values(domain=domain))
        connection.execute(update(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == request_id).values(
                domain_evidence=[authorization['aggregator_bootstrap_pending'],
                    envelope, *domain_proof]))
        store._audit(connection, company_id, 'COMPANY_DOMAIN_ACQUIRED', dict(
            request_id=request_id, method='AGGREGATOR_NATIVE_BOOTSTRAP_V1',
            pending_sha256=envelope['pending_sha256'], receipt_sha256=receipt['sha256'],
            source_snapshot_sha256=receipt['source_snapshot_sha256'],
            employer_response_sha256=receipt['job_response_sha256'],
            apply_response_sha256=receipt['apply_response_sha256']))
    check()
    return {'company_id': company_id, 'domain': domain,
        'request_id': request_id, 'pending_sha256': envelope['pending_sha256'],
        'receipt_sha256': receipt['sha256'], 'domain_evidence': domain_proof}
