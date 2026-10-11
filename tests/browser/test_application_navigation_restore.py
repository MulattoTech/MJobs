"""An explicit navigation choice wins over delayed application restoration."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from starlette.responses import JSONResponse

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationNavigationRestoreTests(unittest.TestCase):
    def test_explicit_profile_navigation_wins_over_delayed_restoration(self):
        for delay_detail in (False, True):
            with self.subTest(delay_detail=delay_detail):
                self.exercise(delay_detail)

    def test_failed_restoration_can_recover_on_refresh(self):
        self.exercise(False, fail_read=True)

    def exercise(self, delay_detail, fail_read=False):
        with tempfile.TemporaryDirectory(prefix='mjobs-navigation-restore-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'test.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(deepcopy(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            application_id = prepare(store, store.jobs('fixture')[0]['id'], 'fixture', root)
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, 'http://127.0.0.1:1', root,
                             'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            started, release, returned = threading.Event(), threading.Event(), threading.Event()
            delayed_path = (f'/profiles/fixture/applications/{application_id}'
                            if delay_detail else '/applications')

            async def delay_read(request, call_next):
                delayed = request.method == 'GET' and request.url.path == delayed_path
                if delayed and fail_read and not started.is_set():
                    started.set()
                    returned.set()
                    return JSONResponse({'detail': 'SYNTHETIC_READ_FAILURE'}, status_code=503)
                if delayed:
                    started.set()
                    while not release.is_set():
                        await asyncio.sleep(.01)
                response = await call_next(request)
                if delayed:
                    returned.set()
                return response

            app.middleware('http')(delay_read)
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated navigation API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script("""
                        localStorage.setItem('mjobs-profile', 'fixture');
                        localStorage.setItem('mjobs-selected-application:fixture', %s);
                    """ % repr(application_id))
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    if fail_read:
                        context.route('**/matches', lambda route: route.fulfill(
                            status=202, content_type='application/json',
                            body='{"operation_id":"navigation-matches"}'))
                        context.route('**/operations/navigation-matches', lambda route: route.fulfill(
                            status=200, content_type='application/json',
                            body='{"id":"navigation-matches","kind":"MATCH","state":"COMPLETED","error":null}'))
                    page = context.new_page()
                    initial_list_reads = []
                    page.on('request', lambda request: initial_list_reads.append(request.url)
                            if urlsplit(request.url).path == '/applications' else None)
                    page.goto(origin)
                    deadline = time.monotonic() + 10
                    while not started.is_set() and time.monotonic() < deadline:
                        page.wait_for_timeout(20)
                    self.assertTrue(started.is_set(), 'Restoration read was not exercised')
                    page.wait_for_timeout(100)
                    self.assertEqual(len(initial_list_reads), 1,
                                     'Initial profile load must have one refresh owner')
                    if fail_read:
                        expect(page.get_by_role('alert').filter(has_text='SYNTHETIC_READ_FAILURE')).to_be_visible()
                        release.set()
                        page.get_by_role('button', name='Refresh matches', exact=True).click()
                        expect(page.get_by_role('heading', name='Every application, accounted for.', exact=True)).to_be_visible()
                        expect(page.get_by_role('region', name='Application tracking', exact=True)).to_be_visible()
                        self.assertEqual(store.application(application_id)['state'], 'PREPARED')
                        self.assertEqual(external, [])
                        context.close()
                        browser.close()
                        return
                    page.get_by_role('navigation', name='Main navigation').get_by_role(
                        'button', name='Profile', exact=True).click()
                    heading = page.get_by_role('heading', name='Your experience, in your words.', exact=True)
                    expect(heading).to_be_visible()
                    release.set()
                    deadline = time.monotonic() + 10
                    while not returned.is_set() and time.monotonic() < deadline:
                        page.wait_for_timeout(20)
                    self.assertTrue(returned.is_set(), 'Delayed response was not released')
                    # Let the released response and React updates settle before checking.
                    page.wait_for_timeout(300)
                    expect(heading).to_be_visible()
                    expect(page.get_by_role('textbox', name='Full name', exact=True)).to_have_value(PROFILE['name'])
                    self.assertEqual(store.application(application_id)['state'], 'PREPARED')
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                release.set()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
