"""Two mounted Add Company forms share durable active discovery after reload."""
from pathlib import Path
from contextlib import ExitStack
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web first')
class CompanyDiscoveryIdempotencyBrowserTests(unittest.TestCase):
    def test_two_tabs_add_same_employer_then_reload_one_durable_operation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'company.db').as_posix()}")
            cleanup.callback(store.engine.dispose)
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            store.save_profile(PROFILE)
            sock = bound_browser_socket()
            cleanup.callback(sock.close)
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, origin, root / 'outputs',
                redis_url='redis://127.0.0.1:1/0',
                operation_mode='PERSONAL')
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', timeout_graceful_shutdown=5))
            thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
            external = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated API did not start')
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
                    pages = [context.new_page(), context.new_page()]
                    operation_ids, request_ids = [], []
                    for page in pages:
                        page.set_default_timeout(10000)
                        page.goto(origin, wait_until='domcontentloaded')
                        page.get_by_role('button', name='Sources', exact=True).click()
                        panel = page.get_by_role('region', name='Add a company to look for jobs')
                        panel.get_by_label('Company name', exact=True).fill('Synthetic Employer')
                        panel.get_by_label('Company website (HTTPS)', exact=True).fill('https://employer.example/')
                        with page.expect_response(lambda response: response.request.method == 'POST'
                                and response.url.endswith('/companies/discovery')) as queued:
                            panel.get_by_role('button', name='Discover company jobs', exact=True).click()
                        self.assertEqual(queued.value.status, 202)
                        operation_ids.append(queued.value.json()['operation_id'])
                        request_ids.append(queued.value.request.post_data_json['request_id'])
                    self.assertNotEqual(request_ids[0], request_ids[1])
                    self.assertEqual(operation_ids[0], operation_ids[1])
                    self.assertEqual(store.count('worker_operations'), 1)
                    page = pages[1]
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Activity', exact=True).click()
                    operations = page.locator('.operation').filter(has_text='COMPANY_DISCOVER')
                    expect(operations).to_have_count(1)
                    expect(operations).to_contain_text(operation_ids[0][:16])
                    expect(operations).to_contain_text('QUEUED')
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=6)
                sock.close()
            self.assertFalse(thread.is_alive(), 'Isolated API must stop')
            self.assertEqual(external, [])
