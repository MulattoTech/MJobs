"""Connected, synthetic browser proof for the read-only resume alignment preview."""
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
from tests.integration.test_spine import PROFILE


class ResumeAlignmentPreviewBrowserTests(unittest.TestCase):
    def test_preview_shows_exact_support_and_unmatched_terms_before_explicit_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'alignment.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE, facts=[
                dict(PROFILE['facts'][0], id='python', text='Python'),
                dict(PROFILE['facts'][0], id='duty', kind='responsibility',
                     text='Administer Linux servers and restore production services.'),
                dict(PROFILE['facts'][1], id='pending-kubernetes', text='Kubernetes', user_approved=False),
            ]))
            with fixture_server() as fixture:
                from services.discovery.pipeline import discover
                discover(store, fixture, 'fixture')
            job = store.jobs()[0]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(
                    skills=['Python', 'Kubernetes'],
                    description=('Responsibilities:\n- Administer Linux servers and restore production services.\n'
                                 '- Coordinate cross-team incident communication and status updates.\n'
                                 'Requirements:\n')))

            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, 'http://127.0.0.1:1', root,
                'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Resume-alignment API did not start')
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []
                    prepare_requests = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    page = context.new_page()

                    def observe_prepare(route):
                        prepare_requests.append(route.request.post_data_json)
                        route.fulfill(status=202, content_type='application/json',
                            body=json.dumps({'operation_id':'synthetic-prepare','application_id':None}))

                    page.route('**/applications', lambda route: observe_prepare(route)
                               if route.request.method == 'POST' else route.continue_())
                    page.route('**/operations/synthetic-prepare', lambda route: route.fulfill(
                        status=200, content_type='application/json', body=json.dumps({
                            'id':'synthetic-prepare','kind':'PREPARE','state':'COMPLETED','error':None,
                            'attempt_count':0,'cancel_requested':False,'updated_at':'2026-10-08T00:00:00Z'})))
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload()
                    page.set_default_timeout(5000)
                    expect(page.locator('.job-grid .job-list .job-card').first).to_be_visible()
                    page.locator('.job-grid .job-list .job-card').first.click()
                    page.get_by_label('Order approved skills for this job', exact=True).check()
                    page.get_by_label('Choose resume facts', exact=True).check()
                    preview_button = page.get_by_role('button', name='Preview keyword alignment', exact=True)
                    expect(preview_button).to_be_visible()
                    prepare_button = page.get_by_role('button', name='Prepare documents', exact=True)
                    expect(prepare_button).to_be_disabled()
                    preview_button.click()
                    preview = page.get_by_role('region', name='Resume keyword alignment preview')
                    expect(preview.get_by_text('Python', exact=True)).to_be_visible()
                    expect(preview).to_contain_text('fact references: python')
                    expect(preview.get_by_text('Kubernetes', exact=True)).to_be_visible()
                    expect(preview.get_by_text('Unmatched skills: Kubernetes')).to_be_visible()
                    expect(preview.get_by_text('Unmatched responsibilities:', exact=False)).to_be_visible()
                    expect(preview).to_contain_text('fact references: duty')
                    expect(prepare_button).to_be_enabled()
                    self.assertEqual(prepare_requests, [])
                    page.get_by_label('Include Python', exact=True).uncheck()
                    expect(preview).to_contain_text('Selected resume facts only.')
                    expect(preview).to_contain_text('Unmatched skills: Python; Kubernetes')
                    expect(preview).not_to_contain_text('fact references: python')
                    expect(preview).to_contain_text('fact references: duty')
                    page.get_by_label('Include Python', exact=True).check()
                    expect(preview).to_contain_text('supported by approved fact references: python')
                    expect(prepare_button).to_be_enabled()
                    page.get_by_role('button', name='Reload approved facts', exact=True).click()
                    expect(page.get_by_text('Read-only preview for this saved job and profile version.', exact=False)).to_be_visible()
                    expect(prepare_button).to_be_enabled()

                    prepare_button.click()
                    self.assertEqual(len(prepare_requests), 1)
                    payload = prepare_requests[0]
                    self.assertTrue(payload['expected_profile_version'])
                    self.assertTrue(payload['expected_job_fingerprint'])
                    self.assertEqual(payload['tailor_fact_ids'], ['python','duty'])
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
