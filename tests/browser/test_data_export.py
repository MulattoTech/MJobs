"""Connected local browser acceptance for profile-scoped data exports."""
import json
from pathlib import Path
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


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class DataExportBrowserTests(unittest.TestCase):
    def test_profile_json_download_is_local_readable_and_profile_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            first = {
                'id': 'export-fixture-one', 'name': 'Export Fixture One',
                'email': 'export-one@example.invalid', 'answers': [],
                'facts': [{'id': 'synthetic-one', 'text': 'fixture-one-only-value',
                           'kind': 'skill', 'source': 'synthetic test fixture',
                           'verified': False, 'user_approved': False, 'confidence': 0}],
            }
            second = {
                'id': 'export-fixture-two', 'name': 'Export Fixture Two',
                'email': 'export-two@example.invalid', 'answers': [],
                'facts': [{'id': 'synthetic-two', 'text': 'fixture-two-only-value',
                           'kind': 'skill', 'source': 'synthetic test fixture',
                           'verified': False, 'user_approved': False, 'confidence': 0}],
            }
            store.save_profile(first)
            store.save_profile(second)
            app = create_app(
                store, 'http://127.0.0.1:1', root,
                'redis://127.0.0.1:1/0', operation_mode='PERSONAL',
            )
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            origin_netloc = urlsplit(origin).netloc
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated data export API did not start')
                    time.sleep(.02)

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    external, errors, requests = [], [], []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != origin_netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.add_init_script("localStorage.setItem('mjobs-profile', 'export-fixture-one')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: requests.append(
                        (request.method, urlsplit(request.url).path)))
                    page.goto(origin, wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    export_panel = page.locator('section[aria-labelledby="data-export-title"]')
                    expect(export_panel).to_be_visible()
                    expect(export_panel.get_by_text('Export contents are never shown here.', exact=True)).to_be_visible()

                    def download_profile(profile_id):
                        with page.expect_download() as download_info:
                            with page.expect_response(
                                lambda response: response.url.endswith(
                                    f'/profiles/{profile_id}/exports/profile.json')) as response_info:
                                page.get_by_role(
                                    'button', name='Download profile JSON', exact=True).click()
                        response = response_info.value
                        self.assertEqual(response.status, 200, response.status_text)
                        download = download_info.value
                        self.assertEqual(download.suggested_filename, f'mjobs-profile-{profile_id}.json')
                        payload = json.loads(Path(download.path()).read_text(encoding='utf-8'))
                        self.assertEqual(payload['profile_id'], profile_id)
                        self.assertEqual(payload['profile']['id'], profile_id)
                        return payload

                    first_export = download_profile('export-fixture-one')
                    self.assertEqual(first_export['profile']['name'], 'Export Fixture One')
                    self.assertIn('fixture-one-only-value', json.dumps(first_export))
                    self.assertNotIn('fixture-two-only-value', json.dumps(first_export))
                    expect(export_panel.get_by_role('status')).to_have_text('Export downloaded.')

                    page.get_by_label('Active profile', exact=True).select_option('export-fixture-two')
                    expect(export_panel).to_be_visible()
                    second_export = download_profile('export-fixture-two')
                    self.assertEqual(second_export['profile']['name'], 'Export Fixture Two')
                    self.assertIn('fixture-two-only-value', json.dumps(second_export))
                    self.assertNotIn('fixture-one-only-value', json.dumps(second_export))

                    export_requests = [path for method, path in requests if '/exports/' in path]
                    self.assertEqual(export_requests, [
                        '/profiles/export-fixture-one/exports/profile.json',
                        '/profiles/export-fixture-two/exports/profile.json',
                    ])
                    self.assertTrue(all(method == 'GET' for method, _ in requests), requests)
                    self.assertEqual(external, [])
                    self.assertEqual(errors, [], errors)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), 'Isolated data export API did not stop')

