"""Bounded evidence for aggregator-to-native discovery; never an authorization.

Collection may call the pure hint extractor for at most its admitted intake.
Only the COMPANY_DISCOVER worker, after current DB authorization, calls the
resolver. It must bind a returned receipt to the durable source/profile/raw
candidate again before granting any domain or enrollment authority. No source
is configured here, no database is opened, and inbound proof fields are ignored.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from services.company_discovery.application_links import _title
from services.company_discovery.domain_acquisition import corroborated_job_page, _apply_page_url
from services.company_discovery.fetch_policy import bounded_get
from services.company_discovery.public_client import (
    MAX_RESPONSE_BYTES, MIN_REQUEST_INTERVAL, PublicCompanyClient, PublicFetchError, _parse_url,
)


POLICY_FIELDS = ('id', 'provider', 'tenant', 'strategy', 'capabilities', 'enabled',
                 'request_timeout', 'rate_limit', 'concurrency')
SOURCE_FIELDS = ('company_id', 'provider', 'tenant', 'endpoint', 'collector_source_id')
AGGREGATORS = {'wwr': 'weworkremotely.com', 'remotive': 'remotive.com'}
NATIVE_HOSTS = {'boards.greenhouse.io', 'job-boards.greenhouse.io'}
MAX_INPUT_BYTES = 512 * 1024
MAX_LINKS = 256
MAX_ELIGIBLE_ITEMS = 2  # Enforced by the collection caller before scheduling I/O.

BOOTSTRAP_SCHEMAS = {
    1: ('AGGREGATOR_BOOTSTRAP_PENDING_V1', 'AGGREGATOR_NATIVE_BOOTSTRAP_V1',
        'AGGREGATOR_NATIVE_BOOTSTRAP_ENVELOPE_V1', 'fixture-aggregator-bootstrap-v1'),
    2: ('AGGREGATOR_BOOTSTRAP_PENDING_V2', 'AGGREGATOR_NATIVE_BOOTSTRAP_V2',
        'AGGREGATOR_NATIVE_BOOTSTRAP_ENVELOPE_V2', 'fixture-aggregator-bootstrap-v2'),
}


def bootstrap_schemas(version):
    if type(version) is not int or version not in BOOTSTRAP_SCHEMAS:
        raise ValueError('DISCOVERY_BOOTSTRAP_PROOF_CHANGED')
    return BOOTSTRAP_SCHEMAS[version]


def validate_verification_policy(value):
    """Validate the exact fixed projection, without creating DB or I/O authority."""
    from packages.domain.employer_verification import verification_policy_snapshot

    if not isinstance(value, dict):
        raise ValueError('EMPLOYER_VERIFICATION_AUTHORIZATION_INVALID')
    expected = verification_policy_snapshot({
        'id': value.get('profile_id'), 'version': value.get('profile_version'),
        'preferences': {'company_discovery': {'company_discovery_enabled': True,
            'employer_verification': {'version': value.get('version'),
                'mode': value.get('mode'), 'activation_id': value.get('activation_id')}}}})
    if (expected is None or set(value) != set(expected)
            or any(type(value[key]) is not type(expected[key]) or value[key] != expected[key]
                   for key in expected)):
        raise ValueError('EMPLOYER_VERIFICATION_AUTHORIZATION_INVALID')
    return expected


def canonical_origin(value):
    parsed = _url(value)
    if parsed.scheme != 'https' or parsed.port not in (None, 443):
        raise ValueError('BOOTSTRAP_URL_INVALID')
    host = _parse_url(value)[1]
    return 'https://' + ('[' + host + ']' if ':' in host else host)


def validate_v2_observations(receipt, binding, hint):
    """Check typed policy, fixed caps and origin accounting, not network truth."""
    try:
        policy = validate_verification_policy(binding['verification_policy'])
        policy_hash = canonical_digest(policy)
        if (binding['verification_policy_sha256'] != policy_hash
                or receipt['verification_policy_sha256'] != policy_hash
                or policy['profile_id'] != binding['profile_id']
                or policy['profile_version'] != binding['profile_version']):
            return False
        limits = receipt['budget']
        if (not isinstance(limits, dict) or set(limits) != {
                'document_requests', 'http_requests', 'reserved_or_received_bytes',
                'max_document_requests', 'max_http_requests', 'max_bytes', 'timeout_seconds'}
                or any(type(limits[key]) is not int for key in (
                    'document_requests', 'http_requests', 'reserved_or_received_bytes',
                    'max_document_requests', 'max_http_requests', 'max_bytes'))
                or not 2 <= limits['document_requests'] <= limits['max_document_requests'] <= 4
                or not 4 <= limits['http_requests'] <= limits['max_http_requests'] <= 10
                or not 0 <= limits['reserved_or_received_bytes'] <= limits['max_bytes'] <= 20 * 1024 * 1024
                or limits['max_bytes'] < 1
                or type(limits['timeout_seconds']) not in (int, float)
                or not 2 <= limits['timeout_seconds'] <= 30):
            return False
        source_origin = canonical_origin(binding['source_snapshot']['endpoint'])
        native_origin = canonical_origin(receipt['native']['endpoint'])
        employer_origin = canonical_origin(receipt['employer_url'])
        if (native_origin != 'https://boards-api.greenhouse.io'
                or len({source_origin, native_origin, employer_origin}) != 3):
            return False
        expected = {native_origin: ('NATIVE_GREENHOUSE', policy_hash),
                    employer_origin: ('EMPLOYER', policy_hash)}
        if hint['candidate_kind'] == 'AGGREGATOR_LISTING':
            expected[source_origin] = ('SOURCE_LISTING', binding['source_policy_sha256'])
        rows = receipt['origin_policies']
        if (not isinstance(rows, list) or len(rows) != len(expected)
                or not all(isinstance(row, dict) and set(row) == {
                    'origin', 'role', 'policy_sha256', 'http_requests'} for row in rows)
                or [row['origin'] for row in rows] != sorted(expected)):
            return False
        counts = {}
        for row in rows:
            if (type(row['origin']) is not str or canonical_origin(row['origin']) != row['origin']
                    or (row['role'], row['policy_sha256']) != expected[row['origin']]
                    or type(row['http_requests']) is not int or not 2 <= row['http_requests'] <= 10):
                return False
            counts[row['origin']] = row['http_requests']
        chain = receipt['discovery_chain']
        if len(chain) != limits['document_requests'] or sum(counts.values()) != limits['http_requests']:
            return False
        documents = {origin: 0 for origin in expected}
        for item in chain:
            origin = canonical_origin(item['url'])
            if origin not in expected or canonical_origin(item['final_url']) != origin:
                return False
            documents[origin] += 1
        if (any(not count or counts[origin] < count + 1 for origin, count in documents.items())
                or not any(item['url'] == receipt['native']['endpoint'] for item in chain)
                or not any(item['url'] == receipt['employer_url'] or item['final_url'] == receipt['employer_url']
                           for item in chain)):
            return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def source_policy_projection(source):
    return {key: source[key] for key in POLICY_FIELDS}


def normalized_projection(normalized):
    attrs = normalized['attributes']
    return {**{key: normalized[key] for key in ('requisition', 'title', 'description', 'application_url')},
            **{key: attrs[key] for key in ('provider', 'tenant', 'company_name')}}


class _Rejected(ValueError):
    pass


class BootstrapRateBudgetIncompatible(PublicFetchError):
    """An authorized source rate cannot fit even the minimum proof read chain."""
    def __init__(self):
        super().__init__('BOOTSTRAP_RATE_BUDGET_INCOMPATIBLE')


def _url(value):
    parsed = _parse_url(value)
    if parsed is None:
        raise _Rejected('BOOTSTRAP_URL_INVALID')
    url, host, _ = parsed
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global or address.is_multicast:
            raise _Rejected('BOOTSTRAP_URL_INVALID')
    except ValueError as error:
        if isinstance(error, _Rejected):
            raise
    if '\\' in value:
        raise _Rejected('BOOTSTRAP_URL_INVALID')
    return url


def _native(value):
    url = _url(value)
    if url.hostname not in NATIVE_HOSTS:
        return None
    match = re.fullmatch(r'/([a-z0-9][a-z0-9_-]{0,63})/jobs/([1-9][0-9]{0,18})/?', url.path)
    if match and not url.query:
        return match.groups()
    if url.path == '/embed/job_app':
        try:
            pairs = parse_qsl(url.query, strict_parsing=True, max_num_fields=3)
        except ValueError:
            raise _Rejected('BOOTSTRAP_NATIVE_LINK_INVALID') from None
        values = dict(pairs)
        if (len(pairs) == 2 and set(values) == {'for', 'token'}
                and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', values['for'])
                and re.fullmatch(r'[1-9][0-9]{0,18}', values['token'])):
            return values['for'], values['token']
    raise _Rejected('BOOTSTRAP_NATIVE_LINK_INVALID')


def _links(html, base, *, native_only=False):
    if not isinstance(html, str) or len(html.encode('utf-8')) > MAX_INPUT_BYTES:
        raise _Rejected('BOOTSTRAP_MARKUP_LIMIT')
    nodes = BeautifulSoup(html, 'html.parser').find_all(['a', 'iframe'])
    if len(nodes) > MAX_LINKS:
        raise _Rejected('BOOTSTRAP_LINK_LIMIT')
    natives, employers = {}, set()
    for node in nodes:
        value = node.get('href' if node.name == 'a' else 'src')
        if not isinstance(value, str):
            continue
        target = urljoin(base, value)
        looks_native = urlsplit(target).hostname in NATIVE_HOSTS
        looks_apply = node.name == 'a' and bool(re.search(r'\b(apply|application|position|job)\b',
            node.get_text(' ', strip=True), flags=re.I))
        if not looks_native and (native_only or not looks_apply):
            continue
        url = _url(target)
        native = _native(target)
        if native:
            natives[native] = target
        elif looks_apply and url.hostname not in {*AGGREGATORS.values(), 'jobs.lever.co'}:
            employers.add(target)
    if len(natives) > 1 or (not natives and len(employers) > 1):
        raise _Rejected('BOOTSTRAP_LINK_AMBIGUOUS')
    if natives:
        return next(iter(natives.values())), 'NATIVE_GREENHOUSE'
    if employers:
        return next(iter(employers)), 'EMPLOYER_PAGE'
    return None


def extract_bootstrap_hint(raw, normalized):
    """Extract only untrusted pointers; this function performs no I/O."""
    try:
        projection = normalized_projection(normalized)
        provider = projection['provider']
        listing = projection['application_url']
        if (provider not in AGGREGATORS or projection['tenant'] != 'default'
                or _url(listing).hostname != AGGREGATORS[provider]
                or not all(isinstance(projection[key], str) and projection[key]
                           for key in ('requisition', 'title', 'company_name'))):
            return None
        raw_url = raw.get('link', raw.get('guid')) if provider == 'wwr' else raw.get('url')
        if listing != raw_url or len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_INPUT_BYTES:
            return None
        candidate = _links(raw.get('description', ''), listing) or (listing, 'AGGREGATOR_LISTING')
        return {'schema': 'AGGREGATOR_BOOTSTRAP_HINT_V1', 'version': 1,
                'provider': provider, 'listing_url': listing,
                'selected_requisition': projection['requisition'],
                'raw_sha256': canonical_digest(raw), 'normalized_sha256': canonical_digest(projection),
                'candidate_url': candidate[0], 'candidate_kind': candidate[1]}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


@dataclass(frozen=True)
class BootstrapBudget:
    max_document_requests: int = 4
    max_http_requests: int = 10
    max_bytes: int = 20 * 1024 * 1024
    timeout_seconds: float = 30.0

    def __post_init__(self):
        for value, maximum in ((self.max_document_requests, 4), (self.max_http_requests, 10),
                               (self.max_bytes, 20 * 1024 * 1024)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('BOOTSTRAP_BUDGET_INVALID')
        if type(self.timeout_seconds) not in (int, float) or not 2 <= self.timeout_seconds <= 30:
            raise ValueError('BOOTSTRAP_BUDGET_INVALID')


class _Budget:
    def __init__(self, limits, check):
        self.limits, self.check = limits, check
        self.deadline = time.monotonic() + limits.timeout_seconds
        self.documents = self.requests = self.bytes = 0
        self.cancelled = None
        self.next_request = 0.0
        self.rate_lanes = {}
        self.origin_policies = None

    def reserve_rate(self, delay, deadline, local_wait, *, lane=None):
        self.guard()
        stamp = time.monotonic()
        previous = self.next_request if lane is None else self.rate_lanes.get(lane, 0.)
        wait = max(local_wait, previous - stamp, 0.)
        if stamp + wait >= min(deadline, self.deadline):
            raise PublicFetchError('BOOTSTRAP_DEADLINE')
        if lane is None:
            self.next_request = stamp + wait + delay
        else:
            self.rate_lanes[lane] = stamp + wait + delay
        return wait

    def require_rate_window(self, policy, *, verification=None, listing_first=False):
        self.guard()
        # Every successful receipt needs the native API document and an
        # independent employer document. Each fresh origin first reads robots:
        # at least four HTTP starts, with three shared rate intervals. Listing,
        # apply, redirect and response time may need more; this lower bound is
        # only a rejection of impossible policy, never a promise of completion.
        minimum_spacing = max(MIN_REQUEST_INTERVAL, 60.0 / policy.rate_limit)
        spacing = 3 * minimum_spacing
        if verification is not None:
            spacing = 3 * max(MIN_REQUEST_INTERVAL, 60.0 / verification.rate_limit)
            if listing_first:
                spacing += minimum_spacing
        if time.monotonic() + spacing >= self.deadline:
            raise BootstrapRateBudgetIncompatible()

    def bind_origin(self, url, role, policy_sha256):
        origin = canonical_origin(url)
        row = dict(origin=origin, role=role, policy_sha256=policy_sha256, http_requests=0)
        existing = self.origin_policies.get(origin)
        if existing is not None and any(existing[key] != row[key]
                                        for key in ('origin', 'role', 'policy_sha256')):
            raise _Rejected('BOOTSTRAP_ORIGIN_POLICY_CHANGED')
        if existing is None:
            if len(self.origin_policies) >= 3 or any(
                    item['role'] == role for item in self.origin_policies.values()):
                raise _Rejected('BOOTSTRAP_ORIGIN_POLICY_CHANGED')
            self.origin_policies[origin] = row

    def guard(self):
        try:
            self.check()
        except BaseException as error:
            self.cancelled = error
            raise
        if time.monotonic() >= self.deadline:
            raise PublicFetchError('BOOTSTRAP_DEADLINE')

    def begin_request(self, url=None):
        self.guard()
        if self.origin_policies is not None:
            origin = canonical_origin(url)
            if origin not in self.origin_policies:
                raise _Rejected('BOOTSTRAP_ORIGIN_POLICY_CHANGED')
        if self.requests >= self.limits.max_http_requests:
            raise PublicFetchError('BOOTSTRAP_REQUEST_LIMIT')
        # Reserve the inherited client's full response cap before any socket is
        # opened. Thus even a failure mid-body cannot exceed the item byte cap.
        if self.bytes + MAX_RESPONSE_BYTES > self.limits.max_bytes:
            raise PublicFetchError('BOOTSTRAP_BYTE_LIMIT')
        self.requests += 1
        self.bytes += MAX_RESPONSE_BYTES
        if self.origin_policies is not None:
            self.origin_policies[origin]['http_requests'] += 1


class BudgetedPublicClient(PublicCompanyClient):
    """Add a shared item budget without changing DNS, robots or redirect policy."""
    def __init__(self, website, *, budget, **kwargs):
        self.item_budget = budget
        super().__init__(website, **kwargs)

    def _request_once(self, url, deadline):
        self.item_budget.begin_request(url)
        response = super()._request_once(url, deadline)
        # Failed requests retain the full reservation; successful reads refund
        # unused bytes, including robots and redirect response bodies.
        self.item_budget.bytes -= MAX_RESPONSE_BYTES - len(response.content)
        self.item_budget.guard()
        return response


@dataclass(frozen=True)
class _ReadPolicy:
    request_timeout: int
    rate_limit: int
    concurrency: int = 1


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Rejected('BOOTSTRAP_JSON_AMBIGUOUS')
        result[key] = value
    return result


def corroborate_aggregator_job(raw, normalized, source_snapshot, profile_snapshot, *,
                              source_policy, check, client_factory=None, budget=None,
                              verification_policy=None, pending_sha256=None, diagnostic=None):
    """Return observations or None; preserve cancellation and infeasible rate errors.

    Call only after staged COMPANY_DISCOVER authorization. Every origin gets a
    separate same-origin public client under one deadline/request/byte budget.
    This receipt does not authorize writes. A caller must recheck its saved DB
    bindings before persisting it and run normal domain/enrollment guards.
    Valid inputs whose inherited rate cannot fit the minimum proof chain raise
    BootstrapRateBudgetIncompatible before client creation or public requests.
    Optional diagnostic is a non-authoritative fixed-code sink; it never
    changes the proof receipt or grants employer authority.
    """
    tracker = _Budget(budget or BootstrapBudget(), check)
    v2 = verification_policy is not None or pending_sha256 is not None

    def stop(code):
        # Only fixed stage codes; never disclose candidate URLs, page bodies,
        # profile data, exception messages or employer assertions.
        if type(diagnostic) is dict:
            diagnostic['stop_code'] = code
        return None

    try:
        tracker.guard()
        hint = extract_bootstrap_hint(raw, normalized)
        if hint is None:
            return stop('HINT_NOT_ELIGIBLE')
        projection = normalized_projection(normalized)
        policy = source_policy_projection(source_policy)
        if any(type(policy[key]) is not int or not low <= policy[key] <= high
               for key, low, high in (('request_timeout', 2, 60), ('rate_limit', 1, 600), ('concurrency', 1, 8))):
            return stop('SOURCE_POLICY_INVALID')
        read_policy = _ReadPolicy(min(10, policy['request_timeout']), min(60, policy['rate_limit']))
        verification_read_policy = None
        if v2:
            from packages.domain.employer_verification import verification_policy_snapshot
            checked_policy = validate_verification_policy(verification_policy)
            if (checked_policy != verification_policy_snapshot(profile_snapshot)
                    or type(pending_sha256) is not str
                    or re.fullmatch(r'[0-9a-f]{64}', pending_sha256) is None):
                return stop('VERIFICATION_POLICY_INVALID')
            verification_read_policy = _ReadPolicy(checked_policy['request_timeout'], checked_policy['rate_limit'])
            tracker.origin_policies = {}
        source = {key: source_snapshot[key] for key in SOURCE_FIELDS}
        profile_id, version = profile_snapshot['id'], profile_snapshot['version']
        profile_policy = (profile_snapshot.get('preferences') or {}).get('company_discovery', {})
        if (set(source_snapshot) != set(SOURCE_FIELDS)
                or not isinstance(profile_id, str) or not profile_id
                or not isinstance(version, str) or re.fullmatch(r'[a-f0-9]{64}', version) is None
                or policy['enabled'] is not True or policy['id'] != source['collector_source_id']
                or policy['provider'] != source['provider'] or policy['tenant'] != source['tenant']
                or source['provider'] != projection['provider'] or source['tenant'] != 'default'
                or not source['company_id']
                or _url(source['endpoint']).hostname != AGGREGATORS[source['provider']]
                or profile_policy.get('company_discovery_enabled', True) is not True):
            return stop('SOURCE_BINDING_INVALID')
        tracker.require_rate_window(read_policy, verification=verification_read_policy,
                                    listing_first=hint['candidate_kind'] == 'AGGREGATOR_LISTING')
        # Hash all immutable inputs before reading; callbacks must not mutate
        # them between authorization and receipt construction.
        inputs_hash = canonical_digest([raw, projection, source, policy, profile_id, version, profile_policy])
        verification_inputs_hash = canonical_digest([verification_policy, pending_sha256]) if v2 else None
        chain, cached, clients = [], {}, {}
        factory = client_factory or BudgetedPublicClient
        with ExitStack() as stack:
            def fetch(url, role):
                tracker.guard()
                parsed = _url(url)
                origin = parsed.scheme, parsed.hostname, parsed.port or 443
                client_policy = read_policy
                reserve_rate = tracker.reserve_rate
                if v2:
                    source_read = role == 'SOURCE_LISTING'
                    canonical = canonical_origin(url)
                    source_origin = canonical_origin(source['endpoint'])
                    native_origin = 'https://boards-api.greenhouse.io'
                    reserved_employer_origins = {'https://' + host for host in {
                        *AGGREGATORS.values(), *NATIVE_HOSTS, 'boards-api.greenhouse.io', 'jobs.lever.co'}}
                    if (role not in {'SOURCE_LISTING', 'NATIVE_GREENHOUSE', 'EMPLOYER'}
                            or source_read and canonical != source_origin
                            or role == 'NATIVE_GREENHOUSE' and (canonical != native_origin or canonical == source_origin)
                            or role == 'EMPLOYER' and (canonical in reserved_employer_origins
                                or canonical in {source_origin, native_origin})):
                        raise _Rejected('BOOTSTRAP_ORIGIN_POLICY_CHANGED')
                    tracker.bind_origin(url, role, canonical_digest(policy if source_read else checked_policy))
                    client_policy = read_policy if source_read else verification_read_policy
                    lane = 'SOURCE' if source_read else 'VERIFICATION'
                    def reserve_rate(delay, deadline, local_wait, *, lane=lane):
                        return tracker.reserve_rate(delay, deadline, local_wait, lane=lane)
                if url in cached:
                    return cached[url]
                if tracker.documents >= tracker.limits.max_document_requests:
                    raise _Rejected('BOOTSTRAP_DOCUMENT_LIMIT')
                if origin not in clients:
                    remaining = tracker.deadline - time.monotonic()
                    if remaining < 2:
                        raise _Rejected('BOOTSTRAP_DEADLINE')
                    client = factory(url, budget=tracker, check=tracker.guard, total_timeout=remaining,
                                     policy=client_policy, reserve_rate=reserve_rate)
                    if not isinstance(client, BudgetedPublicClient) or client.item_budget is not tracker:
                        raise _Rejected('BOOTSTRAP_TRANSPORT_INVALID')
                    stack.callback(client.close)
                    clients[origin] = client
                tracker.documents += 1
                response = bounded_get(clients[origin], url)
                tracker.guard()
                if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
                    raise _Rejected('BOOTSTRAP_RESPONSE_INVALID')
                chain.append({'url': url, 'final_url': str(response.url),
                              'sha256': hashlib.sha256(response.content).hexdigest()})
                cached[url] = response
                return response

            candidate, kind = hint['candidate_url'], hint['candidate_kind']
            if kind == 'AGGREGATOR_LISTING':
                listing_response = fetch(candidate, 'SOURCE_LISTING')
                found = _links(listing_response.text, str(listing_response.url))
                if not found:
                    return stop('LISTING_LINK_MISSING')
                candidate, kind = found
            employer_response = None
            if kind == 'EMPLOYER_PAGE':
                employer_response = fetch(candidate, 'EMPLOYER')
                found = _links(employer_response.text, str(employer_response.url), native_only=True)
                if not found:
                    return stop('EMPLOYER_NATIVE_LINK_MISSING')
                native_link = found[0]
            else:
                native_link = candidate
            tenant, job_id = _native(native_link)
            endpoint = f'https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs/{job_id}'
            native_response = fetch(endpoint, 'NATIVE_GREENHOUSE')
            native = json.loads(native_response.content, object_pairs_hook=_unique_json,
                                parse_constant=lambda _: (_ for _ in ()).throw(_Rejected('BOOTSTRAP_JSON_INVALID')))
            if (not isinstance(native, dict) or type(native.get('id')) is not int
                    or str(native['id']) != job_id or _title(native.get('title')) != _title(projection['title'])):
                return stop('NATIVE_JOB_MISMATCH')
            website = native.get('absolute_url')
            host = _url(website).hostname
            if host in {*AGGREGATORS.values(), *NATIVE_HOSTS, 'boards-api.greenhouse.io', 'jobs.lever.co'}:
                return stop('EMPLOYER_ORIGIN_INVALID')
            if employer_response is not None and website not in {candidate, str(employer_response.url)}:
                return stop('EMPLOYER_URL_MISMATCH')
            employer_response = employer_response or fetch(website, 'EMPLOYER')
            proof = {'website': website, 'tenant': tenant, 'native_job_id': job_id, 'title': native['title']}
            apply_response = None
            if not corroborated_job_page(proof, projection['company_name'], employer_response):
                apply_url = _apply_page_url(proof, employer_response)
                if not apply_url:
                    return stop('EMPLOYER_JOB_PROOF_MISSING')
                apply_response = fetch(apply_url, 'EMPLOYER')
                if not corroborated_job_page(proof, projection['company_name'], employer_response, apply_response):
                    return stop('EMPLOYER_JOB_PROOF_MISSING')
            tracker.guard()
            current = [raw, normalized_projection(normalized), {key: source_snapshot[key] for key in SOURCE_FIELDS},
                       source_policy_projection(source_policy), profile_snapshot['id'], profile_snapshot['version'],
                       (profile_snapshot.get('preferences') or {}).get('company_discovery', {})]
            if canonical_digest(current) != inputs_hash:
                return stop('INPUTS_CHANGED')
            if v2 and canonical_digest([verification_policy, pending_sha256]) != verification_inputs_hash:
                return stop('INPUTS_CHANGED')
            receipt = {'schema': bootstrap_schemas(2 if v2 else 1)[1], 'version': 2 if v2 else 1,
                'execution_scope': 'CONTROLLED_TRANSPORT' if client_factory else 'PUBLIC_READ_ONLY',
                'raw_sha256': hint['raw_sha256'], 'normalized_projection': projection,
                'normalized_sha256': hint['normalized_sha256'], 'selected_requisition': projection['requisition'],
                'source_id': policy['id'], 'source_snapshot': source, 'source_snapshot_sha256': canonical_digest(source),
                'source_policy_sha256': canonical_digest(policy), 'profile_id': profile_id, 'profile_version': version,
                'employer_url': website, 'job_response_sha256': hashlib.sha256(employer_response.content).hexdigest(),
                'apply_url': str(apply_response.url) if apply_response is not None else None,
                'apply_response_sha256': hashlib.sha256(apply_response.content).hexdigest() if apply_response is not None else None,
                'native': {'provider': 'greenhouse', 'tenant': tenant, 'job_id': job_id, 'title': native['title'],
                    'absolute_url': website, 'endpoint': endpoint,
                    'native_response_sha256': hashlib.sha256(native_response.content).hexdigest(),
                    'native_json_sha256': canonical_digest(native)},
                'discovery_chain': chain,
                'budget': {'document_requests': tracker.documents, 'http_requests': tracker.requests,
                           'reserved_or_received_bytes': tracker.bytes, **tracker.limits.__dict__}}
            if v2:
                receipt.update(pending_sha256=pending_sha256,
                    verification_policy_sha256=canonical_digest(checked_policy),
                    origin_policies=[tracker.origin_policies[key] for key in sorted(tracker.origin_policies)])
            receipt['sha256'] = canonical_digest(receipt)
            if type(diagnostic) is dict:
                diagnostic['stop_code'] = 'CORROBORATED'
            return receipt
    except (PublicFetchError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        if tracker.cancelled is not None:
            raise tracker.cancelled
        if isinstance(error, BootstrapRateBudgetIncompatible):
            raise
        if v2 and isinstance(error, PublicFetchError):
            # Preserve the fixed stop code for the restricted worker's V2
            # terminal path. In particular, 503 must not be rescheduled.
            raise
        return stop('FETCH_OR_PARSE_REJECTED')
