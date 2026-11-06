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
from services.company_discovery.public_client import MAX_RESPONSE_BYTES, PublicCompanyClient, PublicFetchError, _parse_url


POLICY_FIELDS = ('id', 'provider', 'tenant', 'strategy', 'capabilities', 'enabled',
                 'request_timeout', 'rate_limit', 'concurrency')
SOURCE_FIELDS = ('company_id', 'provider', 'tenant', 'endpoint', 'collector_source_id')
AGGREGATORS = {'wwr': 'weworkremotely.com', 'remotive': 'remotive.com'}
NATIVE_HOSTS = {'boards.greenhouse.io', 'job-boards.greenhouse.io'}
MAX_INPUT_BYTES = 512 * 1024
MAX_LINKS = 256
MAX_ELIGIBLE_ITEMS = 2  # Enforced by the collection caller before scheduling I/O.


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

    def reserve_rate(self, delay, deadline, local_wait):
        self.guard()
        stamp = time.monotonic()
        wait = max(local_wait, self.next_request - stamp, 0.)
        if stamp + wait >= min(deadline, self.deadline):
            raise PublicFetchError('BOOTSTRAP_DEADLINE')
        self.next_request = stamp + wait + delay
        return wait

    def guard(self):
        try:
            self.check()
        except BaseException as error:
            self.cancelled = error
            raise
        if time.monotonic() >= self.deadline:
            raise PublicFetchError('BOOTSTRAP_DEADLINE')

    def begin_request(self):
        self.guard()
        if self.requests >= self.limits.max_http_requests:
            raise PublicFetchError('BOOTSTRAP_REQUEST_LIMIT')
        # Reserve the inherited client's full response cap before any socket is
        # opened. Thus even a failure mid-body cannot exceed the item byte cap.
        if self.bytes + MAX_RESPONSE_BYTES > self.limits.max_bytes:
            raise PublicFetchError('BOOTSTRAP_BYTE_LIMIT')
        self.requests += 1
        self.bytes += MAX_RESPONSE_BYTES


class BudgetedPublicClient(PublicCompanyClient):
    """Add a shared item budget without changing DNS, robots or redirect policy."""
    def __init__(self, website, *, budget, **kwargs):
        self.item_budget = budget
        super().__init__(website, **kwargs)

    def _request_once(self, url, deadline):
        self.item_budget.begin_request()
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
                              source_policy, check, client_factory=None, budget=None):
    """Return generated observations or None; preserve caller cancellation.

    Call only after staged COMPANY_DISCOVER authorization. Every origin gets a
    separate same-origin public client under one deadline/request/byte budget.
    This receipt does not authorize writes. A caller must recheck its saved DB
    bindings before persisting it and run normal domain/enrollment guards.
    """
    tracker = _Budget(budget or BootstrapBudget(), check)
    try:
        tracker.guard()
        hint = extract_bootstrap_hint(raw, normalized)
        if hint is None:
            return None
        projection = normalized_projection(normalized)
        policy = source_policy_projection(source_policy)
        if any(type(policy[key]) is not int or not low <= policy[key] <= high
               for key, low, high in (('request_timeout', 2, 60), ('rate_limit', 1, 600), ('concurrency', 1, 8))):
            return None
        read_policy = _ReadPolicy(min(10, policy['request_timeout']), min(60, policy['rate_limit']))
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
            return None
        # Hash all immutable inputs before reading; callbacks must not mutate
        # them between authorization and receipt construction.
        inputs_hash = canonical_digest([raw, projection, source, policy, profile_id, version, profile_policy])
        chain, cached, clients = [], {}, {}
        factory = client_factory or BudgetedPublicClient
        with ExitStack() as stack:
            def fetch(url):
                tracker.guard()
                if url in cached:
                    return cached[url]
                if tracker.documents >= tracker.limits.max_document_requests:
                    raise _Rejected('BOOTSTRAP_DOCUMENT_LIMIT')
                parsed = _url(url)
                origin = parsed.scheme, parsed.hostname, parsed.port or 443
                if origin not in clients:
                    remaining = tracker.deadline - time.monotonic()
                    if remaining < 2:
                        raise _Rejected('BOOTSTRAP_DEADLINE')
                    client = factory(url, budget=tracker, check=tracker.guard, total_timeout=remaining,
                                     policy=read_policy, reserve_rate=tracker.reserve_rate)
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
                listing_response = fetch(candidate)
                found = _links(listing_response.text, str(listing_response.url))
                if not found:
                    return None
                candidate, kind = found
            employer_response = None
            if kind == 'EMPLOYER_PAGE':
                employer_response = fetch(candidate)
                found = _links(employer_response.text, str(employer_response.url), native_only=True)
                if not found:
                    return None
                native_link = found[0]
            else:
                native_link = candidate
            tenant, job_id = _native(native_link)
            endpoint = f'https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs/{job_id}'
            native_response = fetch(endpoint)
            native = json.loads(native_response.content, object_pairs_hook=_unique_json,
                                parse_constant=lambda _: (_ for _ in ()).throw(_Rejected('BOOTSTRAP_JSON_INVALID')))
            if (not isinstance(native, dict) or type(native.get('id')) is not int
                    or str(native['id']) != job_id or _title(native.get('title')) != _title(projection['title'])):
                return None
            website = native.get('absolute_url')
            host = _url(website).hostname
            if host in {*AGGREGATORS.values(), *NATIVE_HOSTS, 'boards-api.greenhouse.io', 'jobs.lever.co'}:
                return None
            if employer_response is not None and website not in {candidate, str(employer_response.url)}:
                return None
            employer_response = employer_response or fetch(website)
            proof = {'website': website, 'tenant': tenant, 'native_job_id': job_id, 'title': native['title']}
            apply_response = None
            if not corroborated_job_page(proof, projection['company_name'], employer_response):
                apply_url = _apply_page_url(proof, employer_response)
                if not apply_url:
                    return None
                apply_response = fetch(apply_url)
                if not corroborated_job_page(proof, projection['company_name'], employer_response, apply_response):
                    return None
            tracker.guard()
            current = [raw, normalized_projection(normalized), {key: source_snapshot[key] for key in SOURCE_FIELDS},
                       source_policy_projection(source_policy), profile_snapshot['id'], profile_snapshot['version'],
                       (profile_snapshot.get('preferences') or {}).get('company_discovery', {})]
            if canonical_digest(current) != inputs_hash:
                return None
            receipt = {'schema': 'AGGREGATOR_NATIVE_BOOTSTRAP_V1', 'version': 1,
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
            receipt['sha256'] = canonical_digest(receipt)
            return receipt
    except (PublicFetchError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        if tracker.cancelled is not None:
            raise tracker.cancelled
        return None
