"""An unexpected private read failure is safe in Chromium and reloads cleanly."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
import uvicorn

from apps.api import main as api_main
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class LocalErrorPrivacyBrowserTests(unittest.TestCase):
    def test_private_error_headers_and_saved_record_reload(self):
        with tempfile.TemporaryDirectory(prefix='mjobs-private-error-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            store.save_profile(dict(PROFILE))
            app = api_main.create_app(store, 'http://127.0.0.1:1', root,
                                      'redis://127.0.0.1:1/0', operation_mode='PERSONAL')
            with TestClient(app) as client:
                saved = client.put('/search-profiles', json={
                    'id': 'error-recovery-search', 'profile_id': 'fixture',
                    'name': 'Private synthetic saved search', 'enabled': False})
                self.assertEqual(saved.status_code, 200, saved.text)
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='critical'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            original = api_main.get_search_profile
            calls = []
            def fail_once(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError('synthetic-private-email@example.test resume employment history')
                return original(*args, **kwargs)
            with patch.object(api_main, 'get_search_profile', side_effect=fail_once):
                thread.start()
                try:
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail('Synthetic API did not start')
                        time.sleep(.02)
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        try:
                            page = browser.new_page()
                            response = page.goto(origin + '/search-profiles/error-recovery-search')
                            self.assertEqual(response.status, 500)
                            self.assertEqual(response.headers.get('cache-control'), 'no-store')
                            self.assertEqual(response.headers.get('x-content-type-options'), 'nosniff')
                            self.assertEqual(response.headers.get('referrer-policy'), 'no-referrer')
                            self.assertIn("frame-ancestors 'none'", response.headers.get('content-security-policy', ''))
                            self.assertEqual(response.json(), {'detail': 'Internal server error'})
                            self.assertNotIn('synthetic-private-email', page.content())
                            recovered = page.reload()
                            self.assertEqual(recovered.status, 200)
                            self.assertEqual(recovered.json()['name'], 'Private synthetic saved search')
                            self.assertEqual(len(calls), 2)
                        finally:
                            browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)
                    sock.close()
                    store.engine.dispose()
                    self.assertFalse(thread.is_alive(), 'Synthetic server leaked')
