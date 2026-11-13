"""Controlled transport proofs for the aggregator bootstrap boundary."""
import copy
import json
from unittest.mock import patch

import httpx
import pytest

from services.company_discovery import aggregator_bootstrap as subject
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError


LISTING = 'https://weworkremotely.com/remote-jobs/example-engineer'
EMPLOYER = 'https://example.com/jobs/123'
NATIVE = 'https://boards-api.greenhouse.io/v1/boards/example/jobs/123'
BOARD = 'https://boards.greenhouse.io/example/jobs/123'


def inputs(description=None):
    raw = {'title': 'Example: Engineer', 'link': LISTING, 'guid': LISTING,
           'description': description or f'<a href="{BOARD}">Apply</a>'}
    normalized = {'requisition': 'example-engineer', 'title': 'Engineer',
                  'description': 'An engineering position', 'application_url': LISTING,
                  'attributes': {'provider': 'wwr', 'tenant': 'default', 'company_name': 'Example'}}
    source = {'company_id': 'company-1', 'provider': 'wwr', 'tenant': 'default',
              'endpoint': 'https://weworkremotely.com/remote-jobs.rss', 'collector_source_id': 'source-1'}
    policy = {'id': 'source-1', 'provider': 'wwr', 'tenant': 'default', 'strategy': 'API',
              'capabilities': {}, 'enabled': True, 'request_timeout': 10,
              'rate_limit': 60, 'concurrency': 1}
    profile = {'id': 'profile-1', 'version': 'b' * 64, 'preferences': {}}
    return raw, normalized, source, policy, profile


def employer_html(**changes):
    node = {'@type': 'JobPosting', 'title': 'Engineer', 'identifier': '123',
            'url': EMPLOYER, 'hiringOrganization': {'name': 'Example', 'url': 'https://example.com'}}
    node.update(changes)
    return '<script type="application/ld+json">' + json.dumps(node) + '</script>' + f'<a href="{BOARD}">Apply</a>'


def pages():
    return {NATIVE: httpx.Response(200, json={'id': 123, 'title': 'Engineer', 'absolute_url': EMPLOYER},
                                  request=httpx.Request('GET', NATIVE)),
            EMPLOYER: httpx.Response(200, text=employer_html(), request=httpx.Request('GET', EMPLOYER))}


def resolve(values=None, responses=None, **kwargs):
    raw, normalized, source, policy, profile = values or inputs()
    responses = responses or pages()
    seen = []
    def wire(client, url, deadline):
        seen.append(url)
        if url.endswith('/robots.txt') and url not in responses:
            return httpx.Response(200, text='User-agent: *\nAllow: /', request=httpx.Request('GET', url))
        return responses[url]
    # Use the production client's robots/redirect/deadline policy and replace only
    # its socket read; the resolver explicitly marks injected clients controlled.
    with patch.object(PublicCompanyClient, '_request_once', wire), patch.object(PublicCompanyClient, '_wait'):
        result = subject.corroborate_aggregator_job(raw, normalized, source, profile,
            source_policy=policy, check=kwargs.pop('check', lambda: None),
            client_factory=subject.BudgetedPublicClient, **kwargs)
    return result, seen


class _RateClock:
    def __init__(self):
        self.current = 100.0

    def monotonic(self):
        return self.current

    def sleep(self, seconds):
        self.current += seconds


def resolve_with_real_rate_gate(values, *, budget=None, check=None, clock=None, trace=None):
    """Replace socket reads and time, retaining production rate reservations/waits."""
    raw, normalized, source, policy, profile = values
    clock = clock or _RateClock()
    trace = trace if trace is not None else {}
    trace.update(clients=[], requests=[])
    responses = pages()

    def factory(url, **kwargs):
        trace['clients'].append(url)
        return subject.BudgetedPublicClient(url, **kwargs)

    def wire(client, url, deadline):
        client._check()
        client._wait(deadline)
        client._budget()
        trace['requests'].append((url, clock.monotonic()))
        if url.endswith('/robots.txt'):
            return httpx.Response(200, text='User-agent: *\nAllow: /', request=httpx.Request('GET', url))
        return responses[url]

    with patch.object(subject.time, 'monotonic', clock.monotonic), \
         patch.object(subject.time, 'sleep', clock.sleep), \
         patch.object(PublicCompanyClient, '_request_once', wire):
        return subject.corroborate_aggregator_job(raw, normalized, source, profile,
            source_policy=policy, check=check or (lambda: None),
            client_factory=factory, budget=budget)


@pytest.mark.parametrize('rate,seconds', [(1, 30), (6, 30), (15, 12)])
def test_impossible_rate_window_is_explicit_before_any_client_or_egress(rate, seconds):
    values = inputs()
    values[3]['rate_limit'] = rate
    trace = {}
    with pytest.raises(PublicFetchError, match='^BOOTSTRAP_RATE_BUDGET_INCOMPATIBLE$'):
        resolve_with_real_rate_gate(values, budget=subject.BootstrapBudget(timeout_seconds=seconds), trace=trace)
    assert trace == {'clients': [], 'requests': []}


@pytest.mark.parametrize('rate', [7, 15, 60])
def test_compatible_rate_window_keeps_real_cross_origin_waits(rate):
    values = inputs()
    values[3]['rate_limit'] = rate
    trace = {}
    proof = resolve_with_real_rate_gate(values, trace=trace)
    assert proof['execution_scope'] == 'CONTROLLED_TRANSPORT'
    assert proof['source_policy_sha256'] == subject.canonical_digest(values[3])
    assert len(trace['clients']) == 2 and len(trace['requests']) == 4
    assert [url for url, _ in trace['requests']] == [
        'https://boards-api.greenhouse.io/robots.txt', NATIVE,
        'https://example.com/robots.txt', EMPLOYER]
    stamps = [stamp for _, stamp in trace['requests']]
    assert all(second - first >= 60.0 / rate - 1e-9 for first, second in zip(stamps, stamps[1:]))
    assert stamps[-1] - stamps[0] < proof['budget']['timeout_seconds']


def test_authorization_time_consumes_the_rate_window_without_restarting_deadline():
    values = inputs()
    values[3]['rate_limit'] = 15
    clock, trace = _RateClock(), {}
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks == 1:
            clock.sleep(18)

    with pytest.raises(PublicFetchError, match='^BOOTSTRAP_RATE_BUDGET_INCOMPATIBLE$'):
        resolve_with_real_rate_gate(values, clock=clock, check=check, trace=trace)
    assert trace == {'clients': [], 'requests': []}


@pytest.mark.parametrize('invalid', ['disabled_profile', 'source_binding', 'malformed_hint'])
def test_invalid_inputs_keep_precedence_over_rate_diagnostic(invalid):
    values = inputs()
    values[3]['rate_limit'] = 1
    if invalid == 'disabled_profile':
        values[4]['preferences'] = {'company_discovery': {'company_discovery_enabled': False}}
    elif invalid == 'source_binding':
        values[2]['collector_source_id'] = 'different-source'
    else:
        values[0]['link'] = 'https://other.example/job'
    trace = {}
    assert resolve_with_real_rate_gate(values, trace=trace) is None
    assert trace == {'clients': [], 'requests': []}


def test_authorization_loss_takes_precedence_over_impossible_rate_window():
    values = inputs()
    values[3]['rate_limit'] = 1
    trace, checks = {}, 0
    failure = PublicFetchError('DISCOVERY_AUTHORIZATION_CHANGED')

    def check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise failure

    with pytest.raises(PublicFetchError) as observed:
        resolve_with_real_rate_gate(values, check=check, trace=trace)
    assert observed.value is failure
    assert trace == {'clients': [], 'requests': []}


def test_exact_native_response_and_reciprocal_employer_produce_bound_receipt():
    values = inputs()
    original = copy.deepcopy(values)
    proof, seen = resolve(values)
    assert proof['schema'] == 'AGGREGATOR_NATIVE_BOOTSTRAP_V1'
    assert proof['execution_scope'] == 'CONTROLLED_TRANSPORT'
    assert proof['native']['job_id'] == '123'
    assert proof['employer_url'] == EMPLOYER
    assert proof['raw_sha256'] == subject.canonical_digest(values[0])
    assert proof['sha256'] == subject.canonical_digest({k: v for k, v in proof.items() if k != 'sha256'})
    assert NATIVE in seen and EMPLOYER in seen
    assert values == original


@pytest.mark.parametrize('field,value', [('id', 124), ('id', True), ('title', 'Other role'),
                                      ('absolute_url', 'https://boards.greenhouse.io/example/jobs/123')])
def test_mismatched_native_identity_never_produces_proof(field, value):
    responses = pages()
    native = responses[NATIVE].json()
    native[field] = value
    responses[NATIVE] = httpx.Response(200, json=native, request=httpx.Request('GET', NATIVE))
    proof, _ = resolve(responses=responses)
    assert proof is None


@pytest.mark.parametrize('changes', [{'title': 'Other role'}, {'identifier': '999'},
    {'hiringOrganization': {'name': 'Other company', 'url': 'https://example.com'}},
    {'hiringOrganization': {'name': 'Example', 'url': 'https://attacker.com'}}])
def test_employer_identity_requires_independent_structured_reciprocity(changes):
    responses = pages()
    responses[EMPLOYER] = httpx.Response(200, text=employer_html(**changes), request=httpx.Request('GET', EMPLOYER))
    assert resolve(responses=responses)[0] is None


def test_ambiguous_native_links_are_rejected_without_network():
    values = inputs(f'<a href="{BOARD}">Apply</a><a href="https://boards.greenhouse.io/other/jobs/123">Apply</a>')
    assert subject.extract_bootstrap_hint(values[0], values[1]) is None
    assert resolve(values)[1] == []


def test_pure_listing_fallback_is_only_an_untrusted_pointer():
    raw, normalized, *_ = inputs('<p>No external job links</p>')
    hint = subject.extract_bootstrap_hint(raw, normalized)
    assert hint['schema'] == 'AGGREGATOR_BOOTSTRAP_HINT_V1'
    assert hint['candidate_url'] == LISTING
    assert hint['candidate_kind'] == 'AGGREGATOR_LISTING'
    assert 'native' not in hint and 'employer_url' not in hint


def test_listing_apply_anchor_is_joined_to_native_absolute_url():
    values = inputs('<p>No external job links</p>')
    responses = pages()
    responses[LISTING] = httpx.Response(200, text=f'<a href="{EMPLOYER}">Apply for this position</a>',
                                      request=httpx.Request('GET', LISTING))
    proof, seen = resolve(values, responses)
    assert proof and seen.count(EMPLOYER) == 1
    assert proof['discovery_chain'][0]['url'] == LISTING


def test_missing_native_link_does_not_trigger_a_search_or_guess():
    values = inputs(f'<a href="{EMPLOYER}">Apply</a>')
    responses = pages()
    responses[EMPLOYER] = httpx.Response(200, text='No ATS evidence', request=httpx.Request('GET', EMPLOYER))
    proof, seen = resolve(values, responses)
    assert proof is None and NATIVE not in seen


@pytest.mark.parametrize('bad', ['http://example.com/jobs/123', 'https://127.0.0.1/jobs/123',
    'https://localhost/jobs/123', 'https://example.com:444/jobs/123', 'https://u:p@example.com/jobs/123'])
def test_unsafe_candidate_is_rejected_without_network(bad):
    values = inputs(f'<a href="{bad}">Apply</a>')
    assert resolve(values)[1] == []


def test_cancellation_exception_is_preserved():
    class Cancelled(RuntimeError): pass
    def check(): raise Cancelled('cancelled')
    with pytest.raises(Cancelled):
        resolve(check=check)


def test_request_budget_counts_robots_and_document_reads():
    proof, seen = resolve(budget=subject.BootstrapBudget(max_http_requests=3))
    assert proof is None
    assert len(seen) == 3


def test_total_deadline_is_not_restarted_for_a_new_origin():
    with patch.object(subject.time, 'monotonic', side_effect=[0., 0., 31., 31., 31.]):
        assert resolve()[0] is None


def test_cross_origin_redirect_remains_fail_closed():
    values = inputs()
    responses = pages()
    responses[NATIVE] = httpx.Response(302, headers={'location': 'https://other.example/123'},
                                     request=httpx.Request('GET', NATIVE))
    assert resolve(values, responses)[0] is None


@pytest.mark.parametrize('origin,blocked', [('https://boards-api.greenhouse.io', NATIVE),
                                          ('https://example.com', EMPLOYER)])
def test_robots_denial_stops_the_integrated_traversal(origin, blocked):
    responses = pages()
    robots = origin + '/robots.txt'
    responses[robots] = httpx.Response(200, text='User-agent: *\nDisallow: /', request=httpx.Request('GET', robots))
    proof, seen = resolve(responses=responses)
    assert proof is None and robots in seen and blocked not in seen


def test_byte_budget_refuses_even_the_first_socket_when_reservation_cannot_fit():
    assert resolve(budget=subject.BootstrapBudget(max_bytes=1024))[1] == []


def test_profile_policy_mutation_during_reads_cannot_create_receipt():
    values = inputs()
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls > 8:
            values[4]['preferences'] = {'company_discovery': {'company_discovery_enabled': False}}
    assert resolve(values, check=check)[0] is None


@pytest.mark.parametrize('mutation', ['disabled', 'profile', 'provider', 'collector'])
def test_source_profile_policy_conflicts_rejected_before_network(mutation):
    values = inputs()
    if mutation == 'disabled': values[3]['enabled'] = False
    if mutation == 'profile': values[4]['preferences'] = {'company_discovery': {'company_discovery_enabled': False}}
    if mutation == 'provider': values[3]['provider'] = 'greenhouse'
    if mutation == 'collector': values[2]['collector_source_id'] = 'other'
    assert resolve(values)[1] == []


def test_policy_hash_ignores_collection_telemetry_but_not_policy_changes():
    policy = inputs()[3]
    original = subject.canonical_digest(subject.source_policy_projection(policy))
    policy['last_attempt'] = 'later'
    assert subject.canonical_digest(subject.source_policy_projection(policy)) == original
    policy['rate_limit'] = 2
    assert subject.canonical_digest(subject.source_policy_projection(policy)) != original


def test_stricter_source_limits_are_applied_to_every_origin():
    values = inputs()
    values[3].update(request_timeout=3, rate_limit=15)
    observed = []
    original = subject.BudgetedPublicClient.__init__
    def record(client, *args, **kwargs):
        observed.append(kwargs.get('policy'))
        original(client, *args, **kwargs)
    with patch.object(subject.BudgetedPublicClient, '__init__', record):
        assert resolve(values)[0] is not None
    assert len(observed) == 2
    assert all(policy and policy.request_timeout == 3 and policy.rate_limit == 15
               and policy.concurrency == 1 for policy in observed)


def test_duplicate_json_native_keys_are_not_accepted():
    responses = pages()
    responses[NATIVE] = httpx.Response(200, content=b'{"id":123,"id":124,"title":"Engineer"}',
                                     request=httpx.Request('GET', NATIVE))
    assert resolve(responses=responses)[0] is None
