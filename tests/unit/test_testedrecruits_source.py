import httpx
import pytest

from services.discovery.sources.errors import SourceError
from services.discovery.sources.contract import SourceCapability
from services.discovery.sources.registry import source_adapter
from services.discovery.sources.testedrecruits import TestedRecruits


def listing(*, count=2, rows=None, extra=""):
    rows = rows if rows is not None else [
        ('101', 'Remote Office Assistant', 'Manage the office calendar.'),
        ('102', 'Frontend Developer', 'Build accessible interfaces.'),
    ]
    body = ''.join(
        '<tr><td class="job-title"><a href="https://app.testedrecruits.com/posting/{id}">{title}</a></td>'
        '<td class="job-description"></td><td class="job-description">{description}</td>'
        '<td class="job-apply-btn"><a href="https://app.testedrecruits.com/posting/{id}">Apply</a></td></tr>'.format(
            id=job_id, title=title, description=description,
        ) for job_id, title, description in rows
    )
    return f'''<!doctype html><html><head><title>Acme Company Jobs &amp; Careers</title></head><body>
      <p>Current Job Postings: {count}</p><table>{body}</table>{extra}</body></html>'''.encode()


def adapter_for(body, *, url='https://app.testedrecruits.com/embedJobs/7/acme-company', check=None):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'}, content=body)

    adapter = TestedRecruits('7/acme-company', transport=httpx.MockTransport(respond), check=check)
    return adapter, requests


def actual_public_client_seam(monkeypatch, *, body=None, robots_status=200, listing_status=200):
    """Exercise the real public client while replacing only its socket boundary."""
    from services.company_discovery.public_client import PublicCompanyClient

    calls = []
    closed = []
    real_close = PublicCompanyClient.close

    def request_once(client, url, deadline):
        calls.append(url)
        if url.endswith('/robots.txt'):
            return httpx.Response(robots_status, content=(
                b'User-agent: *\nAllow: /\n' if robots_status == 200 else b''
            ), request=httpx.Request('GET', url))
        return httpx.Response(
            listing_status,
            headers={'content-type': 'text/html'},
            content=body if body is not None else listing(),
            request=httpx.Request('GET', url),
        )

    def close(client):
        closed.append(client)
        real_close(client)

    monkeypatch.setattr(PublicCompanyClient, '_request_once', request_once)
    monkeypatch.setattr(PublicCompanyClient, 'close', close)
    return calls, closed


def test_published_company_listing_has_closed_inventory_and_truthful_unknowns():
    adapter, requests = adapter_for(listing())

    batch = adapter.search()

    assert len(requests) == 1
    assert str(requests[0].url) == 'https://app.testedrecruits.com/embedJobs/7/acme-company'
    assert batch.complete_inventory is True
    assert batch.endpoint == str(requests[0].url)
    assert len(batch.items) == 2
    first = batch.items[0]
    assert first.company.identity_parts == ('testedrecruits', '7')
    assert first.company.name == 'Acme Company'
    assert first.company.domain == ''
    normalized = adapter.normalize(first.raw)
    assert normalized['requisition'] == '101'
    assert normalized['title'] == 'Remote Office Assistant'
    assert normalized['description'] == 'Manage the office calendar.'
    assert normalized['location'] == 'Unknown'
    assert normalized['remote_type'] == 'UNKNOWN'
    assert normalized['application_url'] == 'https://app.testedrecruits.com/posting/101'
    assert normalized['attributes']['salary_min'] is None
    assert normalized['attributes']['salary_max'] is None
    assert normalized['attributes']['listing_description_only'] is True
    assert normalized['attributes']['application_url_status'] == 'PUBLIC_LISTING_LINK'
    assert adapter.capabilities().strategy.value == 'PUBLIC_HTML'
    assert adapter.capabilities().supports(SourceCapability.APPLICATION_REDIRECT) is False


def test_builtin_registry_exposes_the_configured_public_listing_adapter():
    selected = source_adapter({'provider': 'testedrecruits', 'tenant': '7/acme-company'})
    assert isinstance(selected, TestedRecruits)


@pytest.mark.parametrize('tenant', [
    '0/acme', '01/acme', '7/../other', '7/acme?next=x', '7/acme/extra', 'https://other.test/x',
])
def test_invalid_tenant_is_rejected(tenant):
    with pytest.raises(ValueError, match='SOURCE_INVALID_TENANT'):
        TestedRecruits(tenant)


@pytest.mark.parametrize('body', [
    listing(count=3),
    listing(rows=[('101', 'One', 'A'), ('101', 'Duplicate', 'B')]),
    listing(extra='<a rel="next" href="?page=2">Next</a>'),
    b'<html><head><title>Acme Company Jobs &amp; Careers</title></head><body><p>No current openings.</p></body></html>',
    listing(extra='<p>Current Job Postings: 1</p>'),
])
def test_incomplete_or_ambiguous_inventory_is_not_complete(body):
    adapter, _ = adapter_for(body)
    batch = adapter.search()
    assert batch.complete_inventory is False


def test_zero_count_is_complete_but_missing_count_is_not():
    adapter, _ = adapter_for(
        b'<html><head><title>Acme Company Jobs &amp; Careers</title></head>'
        b'<body><p>Current Job Postings: 0</p></body></html>'
    )
    assert adapter.search().complete_inventory is True
    adapter, _ = adapter_for(
        b'<html><head><title>Acme Company Jobs &amp; Careers</title></head>'
        b'<body><p>No current openings.</p></body></html>'
    )
    assert adapter.search().complete_inventory is False


def test_real_public_company_client_api_is_used_without_context_manager(monkeypatch):
    calls, closed = actual_public_client_seam(monkeypatch)
    adapter = TestedRecruits('7/acme-company', request_timeout=45, rate_limit=7, concurrency=3)

    batch = adapter.search()

    assert batch.complete_inventory is True
    assert len(batch.items) == 2
    assert calls == [
        'https://app.testedrecruits.com/robots.txt',
        'https://app.testedrecruits.com/embedJobs/7/acme-company',
    ]
    assert len(closed) == 1
    assert closed[0]._policy.rate_limit == 7
    assert closed[0]._policy.request_timeout == 45
    assert closed[0]._policy.concurrency == 3


@pytest.mark.parametrize('body', [
    listing().replace(b'<title>Acme Company Jobs &amp; Careers</title>', b''),
    listing().replace(b'<title>Acme Company Jobs &amp; Careers</title>', b'<title>Access Denied</title>'),
])
def test_missing_or_unrelated_company_title_cannot_produce_complete_healthy_inventory(monkeypatch, body):
    calls, closed = actual_public_client_seam(monkeypatch, body=body)
    adapter = TestedRecruits('7/acme-company')

    with pytest.raises(SourceError, match='SOURCE_INVALID_FORMAT'):
        adapter.search()

    assert adapter.health() != 'HEALTHY'
    assert len(calls) == 2
    assert len(closed) == 1


def test_truncated_html_is_rejected():
    adapter, _ = adapter_for(
        b'<html><head><title>Acme Company Jobs &amp; Careers</title></head>'
        b'<body><p>Current Job Postings: 2</p>'
    )
    with pytest.raises(SourceError, match='SOURCE_TRUNCATED'):
        adapter.search()


def test_foreign_posting_host_fails_closed():
    body = listing().replace(
        b'https://app.testedrecruits.com/posting/101', b'https://evil.example/posting/101', 1,
    )
    adapter, _ = adapter_for(body)
    with pytest.raises(SourceError, match='SOURCE_INVALID_FORMAT'):
        adapter.search()


def test_normalize_rejects_untrusted_external_posting_url():
    adapter, _ = adapter_for(listing())
    raw = adapter.search().items[0].raw
    raw['posting_url'] = 'https://evil.example/posting/101'
    with pytest.raises(SourceError, match='SOURCE_INVALID_JOB'):
        adapter.normalize(raw)


@pytest.mark.parametrize('company_name', [None, '', 17])
def test_normalize_rejects_missing_empty_or_non_string_observed_company(company_name):
    adapter, _ = adapter_for(listing())
    raw = dict(adapter.search().items[0].raw)
    if company_name is None:
        raw.pop('company_name')
    else:
        raw['company_name'] = company_name

    with pytest.raises(SourceError, match='SOURCE_INVALID_JOB'):
        adapter.normalize(raw)


def test_listing_body_overflow_is_rejected():
    adapter, _ = adapter_for(listing(extra='x' * 2_100_000))
    with pytest.raises(SourceError, match='SOURCE_PAYLOAD_LIMIT'):
        adapter.search()


def test_path_redirect_and_cancellation_are_rejected():
    redirect = httpx.MockTransport(lambda request: httpx.Response(302, headers={'location': 'https://evil.example/'}))
    with pytest.raises(SourceError, match='SOURCE_REDIRECT_BLOCKED'):
        TestedRecruits('7/acme-company', transport=redirect).search()
    with pytest.raises(RuntimeError, match='cancelled'):
        TestedRecruits('7/acme-company', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=listing())
        ), check=lambda: (_ for _ in ()).throw(RuntimeError('cancelled'))).search()


@pytest.mark.parametrize(('failure', 'robots_status', 'listing_status'), [
    ('ROBOTS_DENIED', 403, 200),
    ('HTTP_STATUS_BLOCKED', 200, 403),
])
def test_production_public_client_gates_prevent_denied_pages_from_becoming_empty_inventory(
    monkeypatch, failure, robots_status, listing_status,
):
    calls, closed = actual_public_client_seam(
        monkeypatch, robots_status=robots_status, listing_status=listing_status,
    )
    adapter = TestedRecruits('7/acme-company', request_timeout=45, rate_limit=7, concurrency=3)
    with pytest.raises(SourceError, match=failure):
        adapter.search()
    assert adapter.health() != 'HEALTHY'
    assert len(closed) == 1
    assert len(calls) == (1 if failure == 'ROBOTS_DENIED' else 2)


def test_production_public_client_preserves_cancellation(monkeypatch):
    from services.company_discovery.public_client import PublicCompanyClient

    class Cancelled(RuntimeError):
        pass

    calls = []
    closed = []
    cancel = [False]
    real_close = PublicCompanyClient.close

    def check():
        if cancel[0]:
            raise Cancelled('operation cancelled')

    def request_once(client, url, deadline):
        calls.append(url)
        if url.endswith('/robots.txt'):
            cancel[0] = True
            return httpx.Response(200, content=b'User-agent: *\nAllow: /\n',
                                  request=httpx.Request('GET', url))
        client.check()
        raise AssertionError('cancellation should stop the listing request')

    def close(client):
        closed.append(client)
        real_close(client)

    monkeypatch.setattr(PublicCompanyClient, '_request_once', request_once)
    monkeypatch.setattr(PublicCompanyClient, 'close', close)
    with pytest.raises(Cancelled, match='operation cancelled'):
        TestedRecruits('7/acme-company', check=check).search()
    assert len(calls) == 2
    assert calls[0].endswith('/robots.txt')
    assert calls[1] == 'https://app.testedrecruits.com/embedJobs/7/acme-company'
    assert len(closed) == 1


def test_slow_body_hits_worker_deadline():
    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            import time
            time.sleep(0.03)
            yield listing()

    check = lambda: None
    check.remaining_seconds = lambda: 0.01
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'content-type': 'text/html'}, stream=SlowBody()))
    with pytest.raises(SourceError, match='SOURCE_TIME_LIMIT'):
        TestedRecruits('7/acme-company', transport=transport, check=check).search()
