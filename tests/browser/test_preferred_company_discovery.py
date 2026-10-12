"""Preference UI creates a durable employer-review request with real posting lineage."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.company_discovery.requests import list_discovery_requests
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class PreferredCompanyDiscoveryBrowserTests(unittest.TestCase):
    def test_preference_saves_review_request_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'preferred.db').as_posix()}")
            self.addCleanup(store.engine.dispose)
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            profile = deepcopy(PROFILE)
            profile['facts'] = []
            profile['preferences'] = {'company_discovery': {
                'minimum_match_for_discovery': 100, 'priority_match_for_discovery': 100}}
            store.save_profile(profile)
            company_id = identity('greenhouse', 'example')
            with store.engine.begin() as connection:
                connection.execute(m.companies.insert().values(
                    id=company_id, name='Existing Preferred Employer', domain=''))
            source_id = configure_source(store, 'example')
            def board(request):
                return httpx.Response(200, json={'jobs': [RAW]} if request.url.path.endswith('/jobs')
                    else {'name': 'Existing Preferred Employer'})
            collect_source(store, source_id, 'fixture', transport=httpx.MockTransport(board))
            self.assertEqual(list_discovery_requests(store), [])
            with store.engine.connect() as connection:
                job_id = connection.execute(select(m.jobs.c.id)).scalar_one()
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, origin, root / 'outputs',
                redis_url='redis://127.0.0.1:1/0', operation_mode='PERSONAL')
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', timeout_graceful_shutdown=5))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            external = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated preference API did not start')
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(origin, wait_until='domcontentloaded')
                    card = page.locator('.job-list .job-card').filter(has_text='Existing Preferred Employer')
                    expect(card).to_have_count(1)
                    card.click()
                    panel = page.get_by_role('region', name='Company preference', exact=True)
                    expect(panel).to_contain_text('No explicit choice has been saved')
                    panel.get_by_role('radio', name=re.compile('Prefer this company')).check()
                    with page.expect_response(lambda response: response.request.method == 'PUT'
                            and '/company-preferences/' in response.url) as saved:
                        panel.get_by_role('button', name='Save company preference', exact=True).click()
                    self.assertEqual(saved.value.status, 200)
                    self.assertEqual(saved.value.request.post_data_json['job_id'], job_id)
                    expect(panel).to_contain_text('NEEDS_ORIGIN_REVIEW')
                    request_id = saved.value.json()['discovery']['request_id']
                    page.reload(wait_until='domcontentloaded')
                    card.click()
                    expect(panel).to_contain_text('Saved preference: PREFERRED')
                    expect(panel).to_contain_text('NEEDS_ORIGIN_REVIEW')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    review = page.locator("article[aria-label$='discovery request']").filter(
                        has_text='Existing Preferred Employer')
                    expect(review).to_have_count(1)
                    expect(review.get_by_label('Website to review (HTTPS)')).to_be_visible()
                    self.assertEqual(list_discovery_requests(store)[0]['id'], request_id)
                    self.assertEqual(store.count('worker_operations'), 0)
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=6)
                sock.close()
                store.engine.dispose()
            self.assertEqual(external, [])
