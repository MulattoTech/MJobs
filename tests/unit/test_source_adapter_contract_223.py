"""Registry-wide read-only source contracts for MJ-REQ-222.

All responses are in-memory fixtures. No public request or provider call is made.
The same assertions exercise every built-in adapter, including optional-field
absence and explicit errors for missing required identity.
"""
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx
import pytest

from services.discovery.sources.errors import SourceError
from services.discovery.sources.registry import (
    DEFAULT_SOURCE_ADAPTER_REGISTRY,
    source_adapter,
)

DESCRIPTION = '<p>Build Python systems.</p><script>privateAttack()</script>'
POSTED = '2026-10-20T12:00:00+00:00'
LEVER_ID = '00000000-0000-0000-0000-000000000101'
USAJOBS_IDENTITY = {
    'usajobsControlNumber': 101, 'hiringAgencyCode': 'ZZ01',
    'hiringDepartmentCode': 'ZZ', 'positionOpenDate': '2026-10-20',
    'announcementNumber': 'SYNTHETIC-101',
}
FIXTURES = {
    'usajobs': {'metadata': {**USAJOBS_IDENTITY, 'positionTitle': 'Platform Engineer',
                            'jobcategories': [{'series': '2210'}]},
                'announcement_text': {**USAJOBS_IDENTITY, 'summary': DESCRIPTION}},
    'arbeitnow': {'slug': 'platform-engineer-101', 'company_name': 'Acme',
                  'title': 'Platform Engineer', 'description': DESCRIPTION,
                  'created_at': int(datetime.fromisoformat(POSTED).timestamp()),
                  'url': 'https://www.arbeitnow.com/jobs/companies/acme/platform-engineer-101'},
    'greenhouse': {'id': 101, 'title': 'Platform Engineer', 'content': DESCRIPTION,
                  'first_published_at': POSTED,
                  'absolute_url': 'https://job-boards.greenhouse.io/acme/jobs/101'},
    'lever': {'id': LEVER_ID, 'text': 'Platform Engineer', 'description': DESCRIPTION,
              'hostedUrl': f'https://jobs.lever.co/acme/{LEVER_ID}'},
    'remotive': {'id': 101, 'title': 'Platform Engineer', 'description': DESCRIPTION,
                 'company_name': 'Acme', 'publication_date': POSTED,
                 'url': 'https://remotive.com/remote-jobs/platform-engineer-101'},
    'remoteok': {'id': 101, 'position': 'Platform Engineer', 'description': DESCRIPTION,
                 'company': 'Acme', 'date': POSTED,
                 'url': 'https://remoteok.com/remote-jobs/platform-engineer-101'},
    'wwr': {'title': 'Acme: Platform Engineer', 'description': DESCRIPTION,
            'pubDate': 'Tue, 20 Oct 2026 12:00:00 +0000',
            'guid': 'https://weworkremotely.com/remote-jobs/acme-platform-engineer',
            'link': 'https://weworkremotely.com/remote-jobs/acme-platform-engineer'},
}
MINIMAL_FIELDS = {
    'usajobs': ('metadata', 'announcement_text'),
    'arbeitnow': ('slug', 'company_name', 'title', 'url'),
    'greenhouse': ('id', 'title'),
    'lever': ('id', 'text', 'hostedUrl'),
    'remotive': ('id', 'title'),
    'remoteok': ('id', 'position'),
    'wwr': ('title', 'guid'),
}


def adapter(provider, requests):
    raw = deepcopy(FIXTURES[provider])

    def respond(request):
        requests.append(request)
        assert request.method == 'GET'
        if provider == 'usajobs':
            section = 'announcement_text' if request.url.path.endswith('/announcementtext') else 'metadata'
            payload = {'data': [raw[section]]}
        elif provider == 'arbeitnow':
            payload = {'data': [raw]}
        elif provider == 'greenhouse':
            payload = {'jobs': [raw]} if request.url.path.endswith('/jobs') else {'name': 'Acme'}
        elif provider == 'remotive':
            payload = {'jobs': [raw]}
        elif provider in {'lever', 'remoteok'}:
            payload = [raw]
        else:
            fields = ''.join(f'<{key}>{escape(value)}</{key}>' for key, value in raw.items())
            return httpx.Response(200, content=f'<rss><channel><item>{fields}</item></channel></rss>',
                                  headers={'content-type': 'application/rss+xml'})
        return httpx.Response(200, json=payload)

    tenant = ('default' if provider in {'arbeitnow', 'usajobs'} else
              'global:acme' if provider == 'lever' else 'acme')
    result = source_adapter({'provider': provider, 'tenant': tenant},
                            transport=httpx.MockTransport(respond))
    if provider == 'usajobs':
        result.clock = lambda: datetime.fromisoformat(POSTED)
    return result


def test_contract_fixture_covers_every_builtin_source_adapter():
    assert set(FIXTURES) == set(DEFAULT_SOURCE_ADAPTER_REGISTRY.providers())


@pytest.mark.parametrize('provider', FIXTURES)
def test_source_search_returns_stable_jobs_and_valid_normalized_fields(provider):
    requests = []
    source = adapter(provider, requests)
    first = source.search()
    second = source.search()
    assert len(first.items) == len(second.items) == 1
    normalized = [source.normalize(batch.items[0].raw) for batch in (first, second)]
    assert normalized[0]['requisition'] == normalized[1]['requisition']
    assert normalized[0]['requisition']
    assert first.items[0].company.identity_parts == second.items[0].company.identity_parts
    assert first.items[0].company.name
    for job in normalized:
        assert job['title'] == 'Platform Engineer'
        assert 'Build Python systems.' in job['description']
        assert '<p>' not in job['description']
        assert '<script>' not in job['description']
        assert 'privateAttack' not in job['description']
        url = urlsplit(job['application_url'])
        assert url.scheme == 'https' and url.hostname
        assert url.username is None and url.password is None
        assert job['attributes']['provider'] == provider
        posted = job['attributes']['reported_posted_at']
        if provider in {'lever', 'usajobs'}:
            # These sources provide no exact posting instant; do not invent one.
            assert posted is None
        else:
            assert datetime.fromisoformat(posted).isoformat() == POSTED
    assert source.health() == 'HEALTHY'
    assert not source.capabilities().as_dict()['direct_application']
    assert requests and all(request.method == 'GET' for request in requests)
    if provider == 'arbeitnow':
        assert first.complete_inventory is False and second.complete_inventory is False
        assert all(str(request.url) == 'https://www.arbeitnow.com/api/job-board-api'
                   for request in requests)
    if provider == 'usajobs':
        assert first.complete_inventory is False and second.complete_inventory is False
        assert len(requests) == 4
        assert all(request.url.host == 'data.usajobs.gov' for request in requests)
        assert [request.url.path for request in requests] == [
            '/api/historicjoa', '/api/historicjoa/announcementtext',
        ] * 2


@pytest.mark.parametrize('provider', FIXTURES)
def test_missing_optional_source_fields_preserve_identity_without_inventing_values(provider):
    source = adapter(provider, [])
    minimal = {key: FIXTURES[provider][key] for key in MINIMAL_FIELDS[provider]}
    before = deepcopy(minimal)
    job = source.normalize(minimal)
    assert minimal == before
    assert job['requisition']
    assert job['title'] == 'Platform Engineer'
    if provider == 'usajobs':
        # Joined announcement text is required; the fixture otherwise retains
        # only native identity/title/series and that required text section.
        assert job['description'] == 'Summary\nBuild Python systems.'
        assert 'Python' in job['skills']
    else:
        assert job['description'] == ''
        assert job['skills'] == []
    assert job['attributes']['reported_posted_at'] is None
    for malformed in ({}, None, []):
        with pytest.raises(SourceError):
            source.normalize(malformed)


@pytest.mark.parametrize('url', [
    'https://weworkremotely.com.attacker.invalid/remote-jobs/role',
    'https://notweworkremotely.com/remote-jobs/role',
    'https://@weworkremotely.com/remote-jobs/role',
    'https://weworkremotely.com:invalid/remote-jobs/role',
    'https://weworkremotely.com:444/remote-jobs/role',
    'https://weworkremotely.com\\@attacker.invalid/remote-jobs/role',
    'https://weworkremotely.com/remote-jobs/role\n',
    'http://weworkremotely.com/remote-jobs/role',
])
def test_wwr_rejects_deceptive_or_ambiguous_provider_urls(url):
    source = adapter('wwr', [])
    job = source.normalize({**FIXTURES['wwr'], 'link': url})
    assert job['application_url'] == ''
    assert job['attributes']['application_url_status'] == 'UNAVAILABLE'


@pytest.mark.parametrize('url', [
    'https://weworkremotely.com/remote-jobs/acme-platform-engineer',
    'https://weworkremotely.com:443/remote-jobs/acme-platform-engineer',
])
def test_wwr_preserves_unambiguous_official_provider_urls(url):
    source = adapter('wwr', [])
    assert source.normalize({**FIXTURES['wwr'], 'link': url})['application_url'] == url
