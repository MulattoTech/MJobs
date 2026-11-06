"""First-run helper against Chromium, the real React app/API and local SQLite.

Only a queued fixture discovery is dispatched in-process. No provider, resume
parser substitute, external application, Compose or Redis claim is made here.
"""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from cli.acceptance_onboarding import begin_first_run, observe_first_run
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class FirstRunAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='mjobs-onboarding-helper-')))
        self.store = Store('sqlite:///' + (self.root / 'first-run.db').as_posix())
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.fixture_origin = self.enterContext(fixture_server())
        self.app = create_app(self.store, self.fixture_origin, self.root,
            'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
        socket = bound_browser_socket()
        self.origin = f'http://127.0.0.1:{socket.getsockname()[1]}'
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='error'))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [socket]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.fail('Isolated first-run API did not start')
            time.sleep(.02)
        self.playwright = self.enterContext(sync_playwright())
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.blocked = []
        def local_only(route):
            parsed = urlsplit(route.request.url)
            if parsed.netloc != urlsplit(self.origin).netloc or parsed.path.endswith(('/apply', '/submit', '/mock-run')):
                self.blocked.append((route.request.method, route.request.url))
                route.abort()
            else:
                route.continue_()
        self.context.route('**/*', local_only)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.page.goto(self.origin, wait_until='domcontentloaded')
        self.writes = []
        self.page.on('request', lambda request: self.writes.append((request.method, request.url))
            if request.method != 'GET' else None)

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def begin(self):
        return begin_first_run(self.page, self.origin, acceptance_run_id='isolated-browser-test')

    def test_begin_uses_wizard_and_persists_choices_without_claiming_full_completion(self):
        initial = self.begin()
        self.assertEqual(initial['result'], 'PARTIAL')
        self.assertTrue(initial['first_run_started'])
        self.assertTrue(all(not step['complete'] for step in initial['initial_steps']))
        self.assertEqual(len(self.store.profiles()), 1)
        profile = self.store.profile(initial['profile_id'])
        self.assertEqual(profile['preferences']['work_arrangements'], ['REMOTE'])
        self.assertEqual(profile['facts'], [])
        self.assertEqual(initial['setup_choices']['source_setup'], 'EXPLICITLY_DEFERRED')
        observed = observe_first_run(self.page, self.origin, initial)
        self.assertEqual(observed['result'], 'PARTIAL')
        self.assertIn('run-first-search', observed['pending_steps'])
        self.assertIn('upload-resume', observed['pending_steps'])
        self.assertFalse(observed['checks'][0]['passed'])
        self.assertEqual(self.blocked, [])
        self.assertTrue(all(method == 'PUT' and url.endswith('/profiles') for method, url in self.writes))

    def test_existing_workspace_is_not_relabelled_as_first_run(self):
        initial = self.begin()
        before = self.store.profile(initial['profile_id'])
        self.writes.clear()
        repeated = self.begin()
        self.assertEqual(repeated['result'], 'NOT_RUN')
        self.assertEqual(repeated['reason'], 'FIRST_RUN_WORKSPACE_NOT_FRESH')
        self.assertFalse(repeated['first_run_started'])
        self.assertEqual(self.store.profile(initial['profile_id']), before)
        self.assertEqual(self.writes, [])

    def test_pending_fact_is_not_silently_approved_to_finish_setup(self):
        initial = self.begin()
        self.page.get_by_role('button', name='Add fact', exact=True).click()
        fact = self.page.locator('fieldset.fact').last
        fact.get_by_label('Fact text', exact=True).fill('Incorrect synthetic extraction pending a decision')
        fact.get_by_label('Evidence / source', exact=True).fill('Synthetic local browser fixture')
        self.page.get_by_role('button', name='Save profile', exact=True).click()
        expect(self.page.get_by_role('button', name='Save profile', exact=True)).to_be_enabled()
        observed = observe_first_run(self.page, self.origin, initial)
        self.assertEqual(observed['result'], 'PARTIAL')
        self.assertIn('review-extracted-facts', observed['pending_steps'])
        saved = self.store.profile(initial['profile_id'])['facts'][0]
        self.assertFalse(saved['verified'])
        self.assertFalse(saved['user_approved'])

    def test_complete_wizard_binds_persisted_fixture_search_and_rejects_wrong_lineage(self):
        initial = self.begin()
        wizard = self.page.get_by_role('region', name='First-run setup', exact=True)
        # This test explicitly chooses the supported no-resume/no-facts paths.
        # The connected journey retains its separately tested import/review flow.
        wizard.get_by_role('button', name='Continue without a resume', exact=True).click()
        wizard.get_by_role('button', name='No facts to review yet', exact=True).click()
        wizard.get_by_role('button', name='Open saved searches', exact=True).click()
        self.page.get_by_label('Search name', exact=True).fill('Synthetic Data Engineer first search')
        self.page.get_by_label('Include titles', exact=True).fill('Data Engineer')
        self.page.get_by_label('Remote preference').select_option('REQUIRED')
        with self.page.expect_response(lambda response: response.request.method == 'PUT'
                and urlsplit(response.url).path == '/search-profiles') as saved:
            self.page.get_by_role('button', name='Save search', exact=True).click()
        self.assertEqual(saved.value.status, 200)
        search_id = saved.value.json()['id']
        expect(self.page.get_by_role('button', name='Save search', exact=True)).to_be_enabled()
        self.page.get_by_role('navigation').get_by_role('button', name='Jobs', exact=False).click()
        self.page.get_by_label('Saved search profile', exact=True).last.select_option(search_id)
        self.page.get_by_role('navigation').get_by_role('button', name='Sources', exact=True).click()
        collected = {}
        def dispatch(route):
            collected.update(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 202)
            collected['operation_id'] = response.json()['operation_id']
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.store.url,
                    'MJOBS_OPERATION_MODE': 'FIXTURE', 'MJOBS_OUTPUT_DIR': str(self.root),
                    'MJOBS_MOCK_ORIGIN': self.fixture_origin}):
                outcome = execute_operation(collected['operation_id'])
            self.assertEqual(outcome['status'], 'COMPLETED', outcome)
            route.fulfill(response=response)
        self.context.route(self.origin + '/discovery', dispatch)
        self.page.get_by_role('button', name='Collect local fixture jobs', exact=True).click()
        expect(self.page.get_by_role('status').filter(
            has_text='Local fixture jobs collected for your saved profile. No employer was contacted.')).to_be_visible()
        expect(self.page.get_by_role('button', name='Refresh matches', exact=True)).to_be_enabled()
        observed = observe_first_run(self.page, self.origin, initial, search_profile_id=search_id,
            operation_id=collected['operation_id'], request_id=collected['request_id'])
        self.assertEqual(observed['result'], 'PASS', observed)
        self.assertEqual(observed['completed_steps'], 10)
        self.assertEqual(observed['pending_steps'], [])
        self.assertEqual(observed['profile_id'], initial['profile_id'])
        self.assertEqual(observed['discovery_operation_id'], collected['operation_id'])
        self.assertEqual(observed['search_history']['source'], 'mock-fixture')
        self.assertTrue(observed['checks'][0]['passed'])
        self.assertEqual(len(self.store.profiles()), 1)
        self.assertEqual(self.store.count('applications'), 0)
        self.assertEqual(self.blocked, [])
        wrong = observe_first_run(self.page, self.origin, initial, search_profile_id=search_id,
            operation_id=collected['operation_id'], request_id='wrong-request')
        self.assertEqual(wrong['result'], 'FAIL')
        self.assertEqual(wrong['reason'], 'FIRST_RUN_SEARCH_LINEAGE_MISMATCH')
        self.assertFalse(wrong['checks'][0]['passed'])

    def test_changed_active_profile_cannot_inherit_first_run_receipt(self):
        initial = self.begin()
        self.page.get_by_role('button', name='New profile', exact=True).click()
        expect(self.page.get_by_label('Active profile', exact=True)).to_have_value('')
        observed = observe_first_run(self.page, self.origin, initial)
        self.assertEqual(observed['result'], 'FAIL')
        self.assertEqual(observed['reason'], 'FIRST_RUN_PROFILE_CHANGED')
        self.assertFalse(observed['checks'][0]['passed'])
