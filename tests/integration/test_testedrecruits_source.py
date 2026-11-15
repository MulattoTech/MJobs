from pathlib import Path
import tempfile

import httpx
from sqlalchemy import select

from apps.api.main import SourceInput
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.sources.collect import collect_source, configure_source


def listing(*, count=2, rows=None):
    rows = rows if rows is not None else [
        ('101', 'Remote Office Assistant', 'Manage the office calendar.'),
        ('102', 'Frontend Developer', 'Build accessible interfaces.'),
    ]
    cards = ''.join(
        '<tr><td class="job-title"><a href="https://app.testedrecruits.com/posting/{id}">{title}</a></td>'
        '<td class="job-description"></td><td class="job-description">{description}</td>'
        '<td class="job-apply-btn"><a href="https://app.testedrecruits.com/posting/{id}">Apply</a></td></tr>'.format(
            id=job_id, title=title, description=description,
        ) for job_id, title, description in rows
    )
    return f'''<!doctype html><html><head><title>Acme Company Jobs &amp; Careers</title></head><body>
      <p>Current Job Postings: {count}</p><table>{cards}</table></body></html>'''.encode()


def collect(body, directory):
    store = Store(f"sqlite:///{(Path(directory) / 'testedrecruits.db').as_posix()}")
    store.migrate()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/html'}, content=body)

    transport = httpx.MockTransport(respond)
    request = SourceInput(provider='testedrecruits', tenant='7/acme-company')
    source_id = configure_source(store, **request.model_dump())
    result = collect_source(store, source_id, None, transport=transport,
                           refresh_matches=False)
    return store, source_id, result, requests


def test_configured_listing_uses_real_collector_without_claiming_employer_authority():
    with tempfile.TemporaryDirectory() as directory:
        store, source_id, result, requests = collect(listing(), directory)
        try:
            assert len(requests) == 1
            assert requests[0].method == 'GET'
            assert str(requests[0].url) == 'https://app.testedrecruits.com/embedJobs/7/acme-company'
            assert result['jobs_seen'] == 2
            assert result['rejected'] == 0
            with store.engine.connect() as connection:
                jobs = connection.execute(select(m.jobs).order_by(m.jobs.c.id)).mappings().all()
                postings = connection.execute(select(m.postings).where(
                    m.postings.c.source == 'https://app.testedrecruits.com/embedJobs/7/acme-company'
                )).mappings().all()
                company = connection.execute(select(m.companies).where(
                    m.companies.c.id == identity('testedrecruits', '7')
                )).mappings().one()
                company_source = connection.execute(select(m.company_sources).where(
                    m.company_sources.c.company_id == company['id']
                )).mappings().one()
                assert len(jobs) == len(postings) == 2
                assert {job['requisition'] for job in jobs} == {'101', '102'}
                assert company['name'] == 'Acme Company'
                assert company['domain'] == ''
                assert company_source['provider'] == 'testedrecruits'
                assert company_source['collection_strategy'] == 'PUBLIC_HTML'
                assert company_source['search_url'] == 'https://app.testedrecruits.com/embedJobs/7/acme-company'
                assert company_source['discovered_automatically'] is False
                assert company_source['ats_tenant_id'] is None
                assert connection.execute(select(m.company_ats_tenants)).all() == []
                assert all(posting['raw']['description'] in {
                    'Manage the office calendar.', 'Build accessible interfaces.'
                } for posting in postings)
                assert all(job['location'] == 'Unknown' and job['remote_type'] == 'UNKNOWN'
                           for job in jobs)
                assert all(job['attributes']['listing_description_only'] is True
                           for job in jobs)
        finally:
            store.engine.dispose()


def test_count_mismatch_keeps_collected_rows_without_marking_inventory_complete():
    with tempfile.TemporaryDirectory() as directory:
        store, source_id, result, _ = collect(listing(count=3), directory)
        try:
            assert result['jobs_seen'] == 2
            with store.engine.connect() as connection:
                observation = connection.execute(select(m.source_quality_observations).where(
                    m.source_quality_observations.c.configured_source_id == source_id
                )).mappings().one()
                postings = connection.execute(select(m.postings).where(
                    m.postings.c.source == 'https://app.testedrecruits.com/embedJobs/7/acme-company'
                )).mappings().all()
                assert observation['enumerated'] == 2
                assert len(postings) == 2
        finally:
            store.engine.dispose()
