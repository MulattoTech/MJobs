"""Real local browser exposes historical facts and checksum-backed downloads."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration.test_spine import PROFILE


class DocumentHistoryBrowserTests(unittest.TestCase):
    def test_original_facts_and_three_downloads_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE, facts=[PROFILE['facts'][0]]))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            app_id = prepare(store, store.jobs()[0]['id'], 'fixture', root)
            store.save_profile(dict(PROFILE, facts=[dict(PROFILE['facts'][0], text='Changed Python fact')]))
            row = next(r for r in application_documents(store, app_id) if r['kind']=='resume')
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
            thread.start()
            try:
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline:
                        self.fail('Document browser API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True)
                    context=browser.new_context(accept_downloads=True)
                    external=[]
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url);route.abort()
                        else:
                            route.continue_()
                    context.route('**/*',local_only)
                    page=context.new_page()
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    for _ in range(2):
                        page.reload()
                        page.get_by_role('button',name='Applications',exact=False).first.click()
                        page.locator('.job-card').first.click()
                        document=page.get_by_role('region',name='resume version 1',exact=True)
                        document.get_by_text('Generation details and facts',exact=True).click()
                        expect(document.get_by_text('Generation method: deterministic-fact-copy-v1',exact=True)).to_be_visible()
                        expect(document.get_by_text('Source: fictional-test-profile',exact=False)).to_be_visible()
                        expect(document).not_to_contain_text('Changed Python fact')
                        for fmt in ('pdf','docx','txt'):
                            with page.expect_download() as event:
                                document.get_by_role('link',name='Download '+fmt.upper(),exact=True).click()
                            data=Path(event.value.path()).read_bytes()
                            self.assertEqual(hashlib.sha256(data).hexdigest(),row['sha256'][fmt])
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
