"""Loopback UI error handling for a rejected reviewed rewrite.

The durable backend rejection is exercised in the paired integration test; this
browser test injects that operation failure to verify user-visible recovery.
"""
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration.test_spine import PROFILE


class ResumeTailoringFactsBrowserTests(unittest.TestCase):
    def test_dropping_named_credential_is_reported_and_creates_no_tailored_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'tailoring.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            experience = dict(PROFILE['facts'][0], id='experience', kind='experience',
                text='Supported Cisco CCNA routing for 10 sites since 2020.')
            store.save_profile(dict(PROFILE, facts=[experience]))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            job = store.jobs()[0]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(skills=['Routing']))
            application_id = prepare(store, job['id'], 'fixture', root)

            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            api = create_app(store, 'http://127.0.0.1:1', root,
                'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            server = uvicorn.Server(uvicorn.Config(api, log_level='error'))
            thread = threading.Thread(target=server.run,
                kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Resume-tailoring API did not start')
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)

                    def reject_prepare(route):
                        if route.request.method != 'POST':
                            route.continue_()
                            return
                        route.fulfill(status=202, content_type='application/json', body=json.dumps({
                            'operation_id': 'synthetic-rejected-prepare',
                        }))

                    def rejected_operation(route):
                        route.fulfill(status=200, content_type='application/json', body=json.dumps({
                            'id': 'synthetic-rejected-prepare', 'kind': 'PREPARE', 'state': 'FAILED',
                            'error': 'RESUME_REPHRASE_FACTS_CHANGED: preserve named source tokens',
                            'attempt_count': 1, 'cancel_requested': False,
                            'updated_at': '2026-10-08T00:00:00Z',
                        }))

                    page = context.new_page()
                    page.route(origin + '/applications', reject_prepare)
                    page.route(origin + '/operations/synthetic-rejected-prepare', rejected_operation)
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload()
                    expect(page.locator('.job-grid .job-list .job-card').first).to_be_visible()
                    page.locator('.job-grid .job-list .job-card').first.click()
                    page.get_by_label('Order approved skills for this job', exact=True).check()
                    page.set_default_timeout(5000)
                    expect(page.get_by_label('Choose resume facts', exact=True)).to_be_visible()
                    page.get_by_label('Choose resume facts', exact=True).check()
                    wording = 'Supported network routing for 10 sites since 2020.'
                    page.get_by_label('Rewrite this experience: ' + experience['text'], exact=True).fill(wording)
                    page.get_by_label('I reviewed this rewrite: ' + experience['text'], exact=True).check()
                    page.get_by_role('button', name='Prepare documents', exact=True).click()
                    expect(page.get_by_role('alert')).to_contain_text('RESUME_REPHRASE_FACTS_CHANGED')

                    rows = application_documents(store, application_id)
                    self.assertFalse(any(row['kind'] == 'resume' and row['version'] == 2 for row in rows))
                    self.assertEqual(store.profile('fixture')['facts'][0]['text'], experience['text'])
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
