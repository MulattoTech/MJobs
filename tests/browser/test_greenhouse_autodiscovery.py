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
from tests.integration import test_greenhouse_autodiscovery as contract
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class GreenhouseAutodiscoveryBrowserTests(unittest.TestCase):
    def test_discover_reload_jobs_and_pause_recurring_employer_collection(self):
        case = contract.GreenhouseAutodiscoveryTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        app = create_app(case.store, 'http://127.0.0.1:1', case.root / 'outputs',
                         redis_url='redis://127.0.0.1:1/0', operation_mode='PERSONAL')
        sock = bound_browser_socket()
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        try:
            with patch.dict(os.environ, case.env), \
                    patch.object(PublicCompanyClient, '_request_once', lambda client, url, deadline: case.page(client, url, deadline)), \
                    patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None: case.board(adapter, suffix, params)):
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
                    page.get_by_label('Company name', exact=True).fill('Example Systems')
                    page.get_by_label('Company website (HTTPS)', exact=True).fill('https://employer.example/')
                    with page.expect_response(lambda response: response.request.method == 'POST'
                                              and response.url.endswith('/companies/discovery')) as admission:
                        page.get_by_role('button', name='Discover company jobs', exact=True).click()
                    operation_id = admission.value.json()['operation_id']
                    result = execute_operation(operation_id)
                    self.assertEqual(result['status'], 'COMPLETED', result)
                    page.reload(wait_until='domcontentloaded')
                    page.locator('#active-profile').select_option('fixture')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    card = page.get_by_role('article', name='Example Systems source', exact=True)
                    expect(card).to_have_count(1)
                    expect(card).to_contain_text('Automatic collection')
                    expect(card).to_contain_text('Active')
                    expect(card).to_contain_text('Scheduled by saved searches')
                    with page.expect_response(lambda response: response.request.method == 'PUT'
                                              and response.url.endswith('/enabled')):
                        card.get_by_role('button', name='Disable Example Systems source', exact=True).click()
                    expect(card).to_contain_text('Paused')
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    expect(card).to_contain_text('Paused')
                    with case.store.engine.begin() as connection:
                        connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
                    self.assertEqual(schedule_due_sources(case.store)['scheduled'], [])
                    page.get_by_role('button', name=re.compile(r'^Jobs')).click()
                    expect(page.locator('.job-card').filter(has_text='Python Engineer')).to_be_visible()
                    with case.store.engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.jobs.c.company_id)).scalar_one(), 'canonical-employer')
                    self.assertEqual(external, [])
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive(), 'Isolated browser API must stop')
