"""Connected employer observer: real React/API/worker and isolated SQLite fixtures.

The test dispatches the API-created operation in process, not through Redis.
No live employer/provider or full Compose acceptance is claimed.
"""
import copy
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
from cli.acceptance_employer import begin_employer_observation, observe_employer_journey
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web first')
class EmployerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='mjobs-employer-')))
        self.store = Store('sqlite:///' + (self.root / 'employer.db').as_posix())
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(copy.deepcopy(PROFILE))
        self.fixture_origin = self.enterContext(fixture_server())
        app = create_app(self.store, self.fixture_origin, self.root,
            'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
        self.socket = bound_browser_socket()
        self.origin = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        self.thread = threading.Thread(target=self.server.run,
            kwargs={'sockets': [self.socket]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.fail('Isolated API did not start')
            time.sleep(.02)
        self.playwright = self.enterContext(sync_playwright())
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.blocked = []
        def local_only(route):
            if urlsplit(route.request.url).netloc != urlsplit(self.origin).netloc:
                self.blocked.append(route.request.url)
                route.abort()
            else:
                route.continue_()
        self.context.route('**/*', local_only)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.page.goto(self.origin, wait_until='domcontentloaded')
        self.page.get_by_label('Active profile', exact=True).select_option('fixture')
        self.writes = []
        self.page.on('request', lambda request: self.writes.append(request.url)
            if request.method not in {'GET', 'HEAD'} else None)

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()
        self.assertFalse(self.thread.is_alive())

    def request(self, path):
        response = self.page.request.get(self.origin + path, max_redirects=0)
        self.assertTrue(response.ok, path)
        return response.json()

    def navigate(self, label):
        self.page.get_by_role('navigation').get_by_role('button', name=label, exact=False).click()

    def collect(self, title='Data Engineer'):
        self.navigate('Sources')
        self.page.get_by_label('Search name', exact=True).fill('Employer observation ' + title)
        self.page.get_by_label('Include titles', exact=True).fill(title)
        self.page.get_by_label('Employment type').select_option('')
        with self.page.expect_response(lambda response: response.request.method == 'PUT'
                and urlsplit(response.url).path == '/search-profiles') as saved:
            self.page.get_by_role('button', name='Save search', exact=True).click()
        search_id = saved.value.json()['id']
        expect(self.page.get_by_role('button', name='Save search', exact=True)).to_be_enabled()
        self.navigate('Jobs')
        self.page.get_by_label('Saved search profile', exact=True).select_option(search_id)
        self.navigate('Sources')
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
        expect(self.page.get_by_role('status').filter(has_text=
            'Local fixture jobs collected for your saved profile. No employer was contacted.')).to_be_visible()
        expect(self.page.get_by_role('button', name='Refresh matches', exact=True)).to_be_enabled()
        return collected

    def observe(self, initial, receipt, request=None):
        rows, observed = [], []
        result = observe_employer_journey(self.page, request or self.request, 'fixture',
            self.navigate, lambda step, status, result, **kw: rows.append(
                {'step': step, 'status': status, 'observable_result': result, **kw}),
            initial, receipt, observed.extend)
        return result, rows, observed

    def test_connected_automatic_chain_is_observed_without_inventing_domain_proof(self):
        initial = begin_employer_observation(self.page, self.request, 'fixture')
        self.assertEqual(initial['company_ids'], [])
        self.assertEqual(initial['job_ids'], [])
        receipt = self.collect('IT Support Specialist')
        second = self.collect()
        self.assertNotEqual(second['operation_id'], receipt['operation_id'])
        self.writes.clear()
        result, rows, observed = self.observe(initial, receipt)
        self.assertEqual(result['result'], 'PARTIAL', result)
        self.assertEqual([row['step'] for row in rows], list(range(23, 32)))
        self.assertEqual({row['step']: row['status'] for row in rows},
            {step: 'PARTIAL' if step == 25 else 'PASS' for step in range(23, 32)})
        self.assertEqual(set(observed), set(result['observed_job_ids']))
        self.assertEqual(len(set(observed)), 2)
        self.assertEqual(result['discovery_operation_id'], receipt['operation_id'])
        self.assertEqual(result['source']['provider'], 'mock-ats')
        self.assertIsNone(result['source']['official_domain'])
        self.assertEqual(result['employer_only_posting_ids'], ['employer-B'])
        self.assertEqual(result['duplicate_posting_ids'], ['board-A', 'board-A-syndicated', 'employer-A'])
        self.assertTrue(all(row['evidence_kind'] == 'CONTROLLED_FIXTURE' for row in rows))
        self.assertTrue(all(row['continuity']['profile_id'] == 'fixture' for row in rows))
        self.assertEqual(sorted(self.writes), sorted(self.origin + '/profiles/fixture/jobs/' + job_id + '/view'
            for job_id in observed))
        self.assertEqual(result['employer_discovery_lineage'], {'profile_id': 'fixture',
            'operation_id': receipt['operation_id'], 'request_id': receipt['request_id'],
            'job_ids': sorted(observed)})
        self.assertEqual(self.blocked, [])
        self.assertEqual(self.store.count('applications'), 0)
        self.page.reload(wait_until='domcontentloaded')
        after, reread, _ = self.observe(initial, receipt)
        self.assertEqual(after['source'], result['source'])
        self.assertEqual([r['status'] for r in reread], [r['status'] for r in rows])

    def test_wrong_lineage_and_existing_employer_do_not_earn_unknown_credit(self):
        initial = begin_employer_observation(self.page, self.request, 'fixture')
        receipt = self.collect()
        for changed in ({'request_id': 'other-request'}, {'profile_id': 'other'},
                        {'search_profile_id': 'other-search'}):
            with self.subTest(changed=changed):
                result, rows, observed = self.observe(initial, {**receipt, **changed})
                self.assertEqual(result['result'], 'FAIL')
                self.assertFalse(any(row['status'] == 'PASS' for row in rows))
                self.assertEqual(observed, [])
        repeated = begin_employer_observation(self.page, self.request, 'fixture')
        result, rows, _ = self.observe(repeated, receipt)
        self.assertFalse(any(row['status'] == 'PASS' for row in rows))

    def test_missing_source_provenance_cannot_prove_automatic_discovery(self):
        initial = begin_employer_observation(self.page, self.request, 'fixture')
        receipt = self.collect()
        def without_events(path):
            value = self.request(path)
            if path.startswith('/company-sources/view'):
                for row in value['company_sources']:
                    row['provenance']['events'] = []
            return value
        result, rows, _ = self.observe(initial, receipt, without_events)
        self.assertNotEqual(result['result'], 'PASS')
        self.assertFalse(next(row for row in rows if row['step'] == 24)['checks'][0]['passed'])

        def failed_operation(path):
            value = self.request(path)
            if path.startswith('/operations/'):
                value['state'] = 'FAILED'
            return value
        failed, failed_rows, observed = self.observe(initial, receipt, failed_operation)
        self.assertEqual(failed['result'], 'FAIL')
        self.assertFalse(any(row['status'] == 'PASS' for row in failed_rows))
        self.assertEqual(observed, [])
