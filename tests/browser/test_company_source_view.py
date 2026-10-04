"""Actual browser/API source controls in isolated synthetic PERSONAL data."""
import copy
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class CompanySourceBrowserTests(unittest.TestCase):
    def test_personal_source_controls_persist_and_feedback_layout_remains_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            store.save_profile(copy.deepcopy(PROFILE))
            with store.engine.begin() as c:
                upsert(c, m.companies, {'id':'browser-fixture', 'name':'Browser Fixture Employer', 'domain':''})
            source = upsert_company_job_source(store, company_id='browser-fixture', provider='rss',
                tenant_identifier='browser-fixture', career_url='https://fixture.example/careers',
                feed_url='https://fixture.example/jobs.xml', collection_strategy='RSS',
                confidence=0.65, discovered_automatically=True)
            imported = import_company_feed_jobs(store, company_id='browser-fixture', source_id=source['id'],
                provider='rss', tenant='browser-fixture', endpoint=source['feed_url'], strategy='RSS',
                items=[{'guid':'one','title':'Python Engineer','description':'Python','link':'/careers/one'},
                       {'guid':'two','title':'SQL Analyst','description':'SQL','link':'/careers/two'}], profile_id='fixture')
            self.assertEqual(imported, 2)
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='PERSONAL')
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated browser API did not start')
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(viewport={'width':1365,'height':1000})
                    external = []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until='domcontentloaded')
                    page.locator('#active-profile').select_option('fixture')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    card = page.locator(f'[data-source-id="{source["id"]}"]')
                    expect(card).to_be_visible()
                    expect(card).to_contain_text('Not verified')
                    expect(card).to_contain_text('Not scheduled')
                    expect(card.get_by_text('Imported 2 jobs from 2 feed records. 2 imported jobs had skill matches at import.', exact=True)).to_be_visible()
                    expect(card).to_contain_text('Source quality')
                    expect(card).to_contain_text('Freshness')
                    expect(card.locator('.company-source-history')).to_be_visible()
                    expect(card).to_contain_text('COMPANY_SOURCE_REVIEW_REQUIRED')
                    expect(card).to_contain_text('Monitor this reviewed source')
                    self.assertEqual(card.get_by_role('button', name='Run now', exact=True).count(), 0)
                    for label in ['Rediscover','Verify','Edit']:
                        expect(card.get_by_role('button', name=label+' \u2014 unavailable', exact=True)).to_be_disabled()
                    card.get_by_role('button', name='Pin Browser Fixture Employer source', exact=True).click()
                    expect(card).to_contain_text('USER_PINNED')
                    card.get_by_role('button', name='Disable Browser Fixture Employer source', exact=True).click()
                    expect(card.get_by_role('button', name='Enable Browser Fixture Employer source', exact=True)).to_be_enabled()
                    card.get_by_role('button', name='Watch Browser Fixture Employer source', exact=True).click()
                    expect(card).to_contain_text('USER_WATCHED')
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    expect(card).to_contain_text('USER_PINNED')
                    expect(card).to_contain_text('USER_WATCHED')
                    expect(card.get_by_role('button', name='Enable Browser Fixture Employer source', exact=True)).to_be_enabled()
                    card.get_by_text('Quality limitations', exact=True).click()
                    expect(card).to_contain_text('Historical uptime is observed attempt availability')
                    card.get_by_text('View Evidence', exact=True).click()
                    expect(card.locator('pre')).to_be_visible()
                    expect(card.locator('pre')).to_contain_text('COMPANY_FEED_IMPORT_COMPLETE')
                    for label in ['Unpin','Enable','Unwatch']:
                        card.get_by_role('button', name=f'{label} Browser Fixture Employer source', exact=True).click()
                        expect(page.get_by_role('status').filter(has_text='Company source preference saved.')).to_be_visible()
                    expect(card).to_contain_text('NORMAL')
                    expect(card).to_contain_text('AUTO_DISCOVERED')
                    expect(card.get_by_role('button', name='Disable Browser Fixture Employer source', exact=True)).to_be_enabled()
                    page.set_viewport_size({'width':390,'height':844})
                    expect(card).to_be_visible()
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    expect(page.locator('.layout')).to_have_attribute('data-operation-mode','PERSONAL')
                    self.assertEqual(page.get_by_role('button', name='Submit to local mock', exact=True).count(),0)
                    self.assertEqual(errors,[])
                    self.assertEqual(external,[])
                    output = os.environ.get('MJOBS_UI_EVIDENCE_DIR')
                    if output:
                        target=Path(output); target.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(target/'company-sources-mobile.png'),full_page=True)
                        page.set_viewport_size({'width':1365,'height':1000})
                        page.screenshot(path=str(target/'company-sources-desktop.png'),full_page=True)
                    context.close()
                    browser.close()
                with store.engine.connect() as c:
                    saved=c.execute(select(m.company_sources).where(m.company_sources.c.id==source['id'])).mappings().one()
                self.assertEqual(saved['priority'],'NORMAL')
                self.assertTrue(saved['enabled'])
                self.assertEqual(saved['origin'],'AUTO_DISCOVERED')
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), 'Isolated test API must stop')


if __name__ == '__main__':
    unittest.main()
