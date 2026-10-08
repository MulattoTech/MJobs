"""Browser acceptance for profile-scoped company notes and stale UI isolation."""
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from playwright.sync_api import sync_playwright, expect

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.audit_history import record_company_discovered
from services.company_discovery.sources import upsert_company_job_source
from services.tracking.companies import add_company_note
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class CompanyTrackingBrowserTests(unittest.TestCase):
    def test_notes_reload_and_switching_isolates_drafts_and_late_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            store.save_profile(copy.deepcopy(PROFILE))
            store.save_profile({**PROFILE, 'id':'fixture-two', 'name':'Second Fixture Profile',
                                'email':'second@example.invalid'})
            with store.engine.begin() as c:
                upsert(c, m.companies, {'id':'browser-acme', 'name':'Browser Acme', 'domain':''})
                record_company_discovered(c, 'fixture', 'browser-acme')
                record_company_discovered(c, 'fixture-two', 'browser-acme')
            source = upsert_company_job_source(store, company_id='browser-acme', provider='rss',
                tenant_identifier='browser-acme', career_url='https://fixture.invalid/careers',
                feed_url='https://fixture.invalid/jobs.xml', collection_strategy='RSS',
                confidence=0.9, discovered_automatically=True)
            app = create_app(store, 'http://127.0.0.1:1', root, operation_mode='PERSONAL')
            add_company_note(store, 'fixture', 'browser-acme', 'Saved only for first profile.', 'seed-note')
            sock = bound_browser_socket()
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
                    context = browser.new_context(viewport={'width':1280,'height':900})
                    context.add_init_script("""
                      const realFetch = window.fetch.bind(window);
                      window.releaseCompanyTrackingRead = null;
                      window.fetch = (input, init={}) => {
                        const requestUrl = new URL(typeof input === 'string' ? input : input.url, location.href);
                        if (requestUrl.pathname === '/profiles/fixture/companies/browser-acme/tracking' &&
                            (!init.method || init.method === 'GET')) {
                          return new Promise(resolve => {
                            window.releaseCompanyTrackingRead = () => realFetch(input, init).then(resolve);
                          });
                        }
                        return realFetch(input, init);
                      };
                    """)
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
                    page.wait_for_function('window.releaseCompanyTrackingRead !== null')
                    page.locator('#active-profile').select_option('fixture-two')
                    expect(card.locator('.company-notes')).to_contain_text('No notes saved')
                    page.evaluate('window.releaseCompanyTrackingRead()')
                    expect(card.locator('.company-notes')).not_to_contain_text('Saved only for first profile.')
                    second_note = card.get_by_role('textbox', name='Add a note for Browser Acme')
                    second_note.fill('Unsaved second-profile draft.')
                    page.locator('#active-profile').select_option('fixture')
                    page.wait_for_function('window.releaseCompanyTrackingRead !== null')
                    page.evaluate('window.releaseCompanyTrackingRead()')
                    expect(card.locator('.company-notes')).to_contain_text('Saved only for first profile.')
                    first_note = card.get_by_role('textbox', name='Add a note for Browser Acme')
                    expect(first_note).to_have_value('')
                    page.locator('#active-profile').select_option('fixture-two')
                    expect(card.get_by_role('textbox', name='Add a note for Browser Acme')).to_have_value('')
                    page.locator('#active-profile').select_option('fixture')
                    page.wait_for_function('window.releaseCompanyTrackingRead !== null')
                    page.evaluate('window.releaseCompanyTrackingRead()')
                    expect(card.locator('.company-notes')).to_contain_text('Saved only for first profile.')
                    first_note = card.get_by_role('textbox', name='Add a note for Browser Acme')
                    first_note.fill('Added through the browser.')
                    card.get_by_role('button', name='Save note', exact=True).click()
                    expect(card.locator('.company-notes')).to_contain_text('Added through the browser.')
                    page.reload(wait_until='domcontentloaded')
                    page.locator('#active-profile').select_option('fixture')
                    page.get_by_role('button', name='Sources', exact=True).click()
                    reloaded_card = page.locator(f'[data-source-id="{source["id"]}"]')
                    page.wait_for_function('window.releaseCompanyTrackingRead !== null')
                    page.evaluate('window.releaseCompanyTrackingRead()')
                    expect(reloaded_card.locator('.company-notes')).to_contain_text('Saved only for first profile.')
                    expect(reloaded_card.locator('.company-notes')).to_contain_text('Added through the browser.')
                    self.assertEqual(external, [])
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()
