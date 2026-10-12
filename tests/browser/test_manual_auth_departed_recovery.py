"""Actual mounted API death, retained Chromium closure and separate retry."""
import json
import re
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import select, update
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.browser_process import owner_process_status
from tests.fixtures.manual_auth_isolated_owner import controlled_job
from tests.integration import test_manual_auth_session as sessions
from tests.unit.test_browser_tree_containment import eventually


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows creator-independent recovery')
class ManualAuthDepartedRecoveryTests(unittest.TestCase):
    setUp = sessions.ManualAuthSessionApiTests.setUp
    tearDown = sessions.ManualAuthSessionApiTests.tearDown

    def recover_and_retry_in_browser(self, recovery):
        from apps.api.main import create_app
        from fixtures.browser_ports import bound_browser_socket
        from playwright.sync_api import expect, sync_playwright
        from urllib.parse import urlsplit
        import uvicorn
        sock = bound_browser_socket()
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(create_app(self.store, self.origin,
            self.root, operation_mode='FIXTURE'), log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        errors, external = [], []
        try:
            thread.start()
            eventually(lambda: server.started, 10)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context()
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    context.add_init_script("localStorage.setItem('mjobs-profile','fixture');"
                        + 'localStorage.setItem(' + json.dumps('mjobs-selected-application:fixture')
                        + ',' + json.dumps(self.application_id) + ');')
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until='domcontentloaded')
                    expect(page.get_by_label('Active profile', exact=True)).to_have_value('fixture')
                    page.get_by_role('navigation', name='Main navigation').get_by_role(
                        'button', name=re.compile('^Applications')).click()
                    page.get_by_role('region', name='Application pipeline').get_by_role(
                        'button', name=re.compile('AUTH_REQUIRED')).click()
                    panel = page.get_by_role('region', name='Manual MOCK authentication')
                    expect(panel).to_be_visible()
                    expect(panel.get_by_role('button', name='Open MOCK sign-in', exact=True)).to_be_disabled()
                    expect(panel.get_by_role('button', name='Rerun MOCK application', exact=True)).to_be_disabled()
                    with page.expect_response(lambda r: r.url == origin + recovery and r.request.method == 'POST') as pending:
                        panel.get_by_role('button', name='Recover closed browser', exact=True).click()
                    response = pending.value
                    self.assertEqual(response.status, 200, response.text())
                    result = response.json()
                    expect(panel.get_by_role('button', name='Recover closed browser', exact=True)).to_have_count(0)
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('navigation', name='Main navigation').get_by_role(
                        'button', name=re.compile('^Applications')).click()
                    page.get_by_role('region', name='Application pipeline').get_by_role(
                        'button', name=re.compile('AUTH_REQUIRED')).click()
                    expect(panel.get_by_role('button', name='Open MOCK sign-in', exact=True)).to_be_enabled()
                    with patch('services.applications.manual_auth_isolated.BrowserTreeJob', controlled_job(self.root)):
                        with page.expect_response(lambda r: r.url == origin + self.url and r.request.method == 'POST') as retry:
                            panel.get_by_role('button', name='Open MOCK sign-in', exact=True).click()
                        self.assertEqual(retry.value.status, 200, retry.value.text())
                    expect(panel.get_by_role('button', name='Rerun MOCK application', exact=True)).to_be_enabled()
                    self.assertFalse(errors, errors)
                    self.assertFalse(external, external)
                    return result
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()

    def test_api_departure_closed_tree_recovery_reload_then_explicit_retry(self):
        before = self.store.application(self.application_id)
        attempts = self.store.count('application_attempts')
        creator = subprocess.Popen([sys.executable, 'tests/fixtures/manual_auth_isolated_owner.py'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        creator.stdin.write(json.dumps({'directory': str(self.root), 'origin': self.origin,
            'url': str(self.store.engine.url), 'path': self.url}) + '\n')
        creator.stdin.close()
        operation = None
        try:
            eventually(lambda: (self.root / 'host-ready').exists(), 25)
            with self.store.engine.connect() as c:
                row = c.execute(select(m.operations).where(m.operations.c.kind == 'MANUAL_AUTH')).mappings().one()
            operation = row['id']
            owner = row['recovery_metadata']['manual_auth_owner']
            self.assertEqual(owner_process_status(owner['api_process']), 'LIVE')
            self.assertIn('tree_retention', owner)
            self.assertNotIn('tree_closure', owner)
            (self.root / 'exit-api').touch()
            self.assertEqual(creator.wait(timeout=10), 0)
            self.assertEqual(owner_process_status(owner['api_process']), 'EXITED')
            # Synthetic lease expiry is deliberately not closure authority.
            with self.store.engine.begin() as c:
                c.execute(update(m.operations).where(m.operations.c.id == operation).values(
                    lease_expires_at='2000-01-01T00:00:00+00:00'))
            self.assertEqual(self.client.post(self.url).status_code, 409)
            recovery = f'{self.url}/{operation}/recover'
            self.assertEqual(self.client.post(recovery).status_code, 409)
            (self.root / 'close-browser').touch()
            def certificate():
                with self.store.engine.connect() as c:
                    metadata = c.execute(select(m.operations.c.recovery_metadata).where(
                        m.operations.c.id == operation)).scalar_one()
                return metadata['manual_auth_owner'].get('tree_closure')
            proof = eventually(certificate, 25)
            self.assertGreaterEqual(proof['accounting']['total'], 4)
            self.assertEqual(proof['accounting']['active'], 0)
            self.assertEqual(proof['accounting']['terminated'], 0)
            with self.store.engine.connect() as c:
                closed_row = c.execute(select(m.operations).where(
                    m.operations.c.id == operation)).mappings().one()
            from services.applications.manual_auth_recovery import confirmed_tree_closure
            self.assertTrue(confirmed_tree_closure(closed_row), 'Actual persisted closure must validate')
            self.assertEqual(owner_process_status(owner['api_process']), 'EXITED',
                             'Original API identity must still prove exited after closure')
            self.assertEqual((closed_row['state'], closed_row['error']),
                             ('RUNNING', None))
            recovered = self.recover_and_retry_in_browser(recovery)
            self.assertEqual(recovered['status'], 'BROWSER_CAPACITY_RECOVERED')
            self.assertEqual(self.store.application(self.application_id), before)
            self.assertEqual(self.store.count('application_attempts'), attempts)
            peer = Store(str(self.store.engine.url))
            from fastapi.testclient import TestClient
            from apps.api.main import create_app
            try:
                with TestClient(create_app(peer, self.origin, self.root, operation_mode='FIXTURE')) as reopened:
                    self.assertEqual(reopened.post(recovery).json(), recovered)
            finally:
                peer.engine.dispose()
            with self.store.engine.connect() as c:
                rows = c.execute(select(m.operations).where(m.operations.c.kind == 'MANUAL_AUTH')).mappings().all()
                events = c.execute(select(m.audit).where(m.audit.c.entity_id == operation,
                    m.audit.c.action == 'MANUAL_AUTH_BROWSER_RECOVERED')).mappings().all()
            self.assertEqual(len(rows), 2)
            old = next(r for r in rows if r['id'] == operation)
            new = next(r for r in rows if r['id'] != operation)
            self.assertEqual((old['state'], old['error']), ('FAILED', 'BROWSER_OWNER_RECOVERED'))
            self.assertEqual(new['state'], 'COMPLETED')
            self.assertEqual(len(events), 1)
            self.assertEqual(self.store.application(self.application_id), before)
            self.assertEqual(self.store.count('application_attempts'), attempts)
            import httpx
            self.assertEqual(httpx.get(self.origin + '/_test/request-counts').json(), {'review': 0, 'submit': 0})
        finally:
            (self.root / 'close-browser').touch()
            (self.root / 'exit-api').touch()
            creator.wait(timeout=15)
