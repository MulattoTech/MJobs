"""Browser -> durable discovery -> existing collector -> reload -> source pause."""
import os
import re
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.scheduler import schedule_due_sources
from services.discovery.sources.greenhouse import Greenhouse
from services.discovery.sources.lever import Lever
from tests.integration import test_greenhouse_autodiscovery as contract
from tests.integration import test_reviewed_greenhouse_enrollment as reviewed_contract
from tests.integration import test_lever_autodiscovery as lever_contract
from tests.unit.test_lever_source import posting
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class GreenhouseAutodiscoveryBrowserTests(unittest.TestCase):
    def test_discover_reload_jobs_and_pause_recurring_employer_collection(self):
        self._journey()

    def test_official_board_link_discovery_reload_and_pause(self):
        self._journey('<a href="https://job-boards.greenhouse.io/example">Open jobs</a>')

    def test_official_board_iframe_discovery_reload_and_pause(self):
        self._journey('<iframe src="https://boards.greenhouse.io/embed/job_board?for=example"></iframe>')

    def test_new_aggregator_employer_review_enrollment_reload_and_pause(self):
        self._journey(reviewed=True)

    def test_reviewed_lever_global_discovery_reload_and_pause(self):
        self._journey(reviewed=True,lever_region='global')

    def test_reviewed_lever_eu_discovery_reload_and_pause(self):
        self._journey(reviewed=True,lever_region='eu')

    def _journey(self, markup=None, reviewed=False, lever_region=None):
        case = (lever_contract.LeverAutodiscoveryTests() if lever_region else
                reviewed_contract.ReviewedGreenhouseEnrollmentTests() if reviewed
                else contract.GreenhouseAutodiscoveryTests())
        case.setUp()
        self.addCleanup(case.tearDown)
        adapter_type = Lever if lever_region else Greenhouse
        source_tenant = 'reviewed'
        if lever_region:
            case.region=lever_region
            case.jobs=[posting(host='jobs.eu.lever.co' if lever_region=='eu' else 'jobs.lever.co')]
            source_tenant=case.tenant
        if reviewed:
            request = case.collect()
            company_name = request['company_name']
        else:
            company_name = 'Example Systems'
        if markup is not None:
            case.markup = markup
        app = create_app(case.store, 'http://127.0.0.1:1', case.root / 'outputs',
                         redis_url='redis://127.0.0.1:1/0', operation_mode='PERSONAL')
        sock = bound_browser_socket()
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        try:
            with patch.dict(os.environ, case.env), \
                    patch.object(PublicCompanyClient, '_request_once', lambda client, url, deadline: case.page(client, url, deadline)), \
                    patch.object(adapter_type, '_get', lambda adapter, suffix, params=None: case.board(adapter, suffix, params)):
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() >= deadline:
                        self.fail('Isolated browser API did not start')
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={'width': 1365, 'height': 1000})
                    external, errors = [], []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until='domcontentloaded')
                    page.locator('#active-profile').select_option('fixture')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    if reviewed:
                        review = page.get_by_role('article', name=company_name + ' discovery request', exact=True)
                        review.get_by_label('Website to review (HTTPS)', exact=True).fill('https://employer.example/')
                        review.get_by_label('I confirm this website belongs to ' + company_name, exact=True).check()
                        expect(review).to_contain_text('recurring collection')
                    else:
                        page.get_by_label('Company name', exact=True).fill(company_name)
                        page.get_by_label('Company website (HTTPS)', exact=True).fill('https://employer.example/')
                    with page.expect_response(lambda response: response.request.method == 'POST'
                                              and response.url.endswith('/review' if reviewed else '/companies/discovery')) as admission:
                        if reviewed:
                            review.get_by_role('button', name='Review website and discover', exact=True).click()
                        else:
                            page.get_by_role('button', name='Discover company jobs', exact=True).click()
                    operation_id = admission.value.json()['operation_id']
                    result = execute_operation(operation_id)
                    self.assertEqual(result['status'], 'COMPLETED', result)
                    page.reload(wait_until='domcontentloaded')
                    page.locator('#active-profile').select_option('fixture')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    card = page.get_by_role('article', name=company_name + ' source', exact=True)
                    if reviewed:
                        card = card.filter(has_text='Automatic collection')
                    expect(card).to_have_count(1)
                    expect(card).to_contain_text('Automatic collection')
                    expect(card).to_contain_text('Active')
                    expect(card).to_contain_text('Scheduled by saved searches')
                    if reviewed:
                        expect(card).to_contain_text('User-confirmed employer identity. Not independently verified.')
                        expect(card).to_contain_text('Not verified')
                        self.assertEqual(case.store.count('company_domains'), 0)
                    with page.expect_response(lambda response: response.request.method == 'PUT'
                                              and response.url.endswith('/enabled')):
                        card.get_by_role('button', name='Disable ' + company_name + ' source', exact=True).click()
                    expect(card).to_contain_text('Paused')
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    expect(card).to_contain_text('Paused')
                    with case.store.engine.begin() as connection:
                        connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
                    scheduled = schedule_due_sources(case.store)['scheduled']
                    if reviewed:
                        with case.store.engine.connect() as connection:
                            collector_id = connection.execute(select(m.company_sources.c.collector_source_id).where(
                                m.company_sources.c.tenant == source_tenant)).scalar_one()
                        self.assertFalse(any(row['source_id'] == collector_id for row in scheduled))
                    else:
                        self.assertEqual(scheduled, [])
                    page.get_by_role('button', name=re.compile(r'^Jobs')).click()
                    expect(page.locator('.job-card').filter(has_text='Senior Python Engineer' if reviewed else 'Python Engineer')).to_be_visible()
                    if lever_region:
                        expect(page.get_by_label('Application provider',exact=True).locator('option[value="lever"]')).to_have_count(1,timeout=1000)
                        page.get_by_label('Application provider',exact=True).select_option('lever')
                        page.get_by_label('ATS',exact=True).select_option('lever')
                        expect(page.locator('.job-card').filter(has_text='Senior Python Engineer')).to_be_visible()
                        page.get_by_role('button',name='Sources',exact=True).click()
                        page.get_by_label('Source provider',exact=True).select_option('lever')
                        page.get_by_label('Lever region',exact=True).select_option(lever_region)
                        page.get_by_label('Lever site',exact=True).fill('second-example')
                        with page.expect_response(lambda response: response.request.method=='PUT' and response.url.endswith('/sources')) as saved:
                            page.get_by_role('button',name='Add board',exact=True).click()
                        self.assertEqual(saved.value.status,200)
                        page.reload(wait_until='domcontentloaded')
                        page.get_by_role('button',name='Sources',exact=True).click()
                        expect(page.locator('section.panel').filter(has_text=f'lever / {lever_region}:second-example')).to_have_count(1)
                    with case.store.engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.jobs.c.company_id)).scalar_one(),
                            case.company_id if reviewed else 'canonical-employer')
                    self.assertEqual(external, [])
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive(), 'Isolated browser API must stop')
