"""Connected Chromium acceptance for explicit fresh-profile recovery."""
import copy
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
from services.data_export import export_profile_backup
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class ProfileRestoreBrowserTests(unittest.TestCase):
    def test_preview_confirm_restore_and_reload_reads_new_profile(self):
        with tempfile.TemporaryDirectory(prefix='mjobs-profile-restore-browser-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            source = copy.deepcopy(PROFILE)
            source.update(id='restore-browser-source', name='Synthetic Restore Source',
                          email='restore-source@example.invalid')
            store.save_profile(source)
            backup = export_profile_backup(store, source['id'], output_dir=root)
            app = create_app(
                store, 'http://127.0.0.1:1', root,
                'redis://127.0.0.1:1/0', operation_mode='PERSONAL',
            )

            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            local_netloc = urlsplit(origin).netloc
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated profile restore API did not start')
                    time.sleep(.02)

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(viewport={'width': 1280, 'height': 1000})
                    external, errors, requests = [], [], []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != local_netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.add_init_script(
                        "localStorage.setItem('mjobs-profile', 'restore-browser-source')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: requests.append(
                        (request.method, urlsplit(request.url).path)))
                    page.goto(origin, wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()

                    backup_file = page.get_by_label(
                        'Choose a JSON backup file (maximum 5 MiB)', exact=True)
                    backup_file.set_input_files({
                        'name': 'synthetic-profile-backup.json',
                        'mimeType': 'application/json',
                        'buffer': json.dumps(backup).encode('utf-8'),
                    })
                    with page.expect_response(lambda response:
                            response.request.method == 'POST'
                            and response.url.endswith('/profile-restores/preview')) as preview_response:
                        page.get_by_role('button', name='Preview backup', exact=True).click()
                    self.assertEqual(preview_response.value.status, 200, preview_response.value.text)
                    preview = page.get_by_role(
                        'region', name='Restore a profile backup', exact=True)
                    expect(preview).to_contain_text('Synthetic Restore Source')
                    confirm = page.get_by_label(
                        'I understand this creates a new profile and imported information must be reviewed.',
                        exact=True,
                    )
                    expect(confirm).to_be_visible()
                    restore_button = page.get_by_role(
                        'button', name='Restore into a new profile', exact=True)
                    expect(restore_button).to_be_disabled()
                    confirm.check()
                    expect(restore_button).to_be_enabled()
                    with page.expect_response(lambda response:
                            response.request.method == 'POST'
                            and response.url.endswith('/profile-restores')) as restore_response:
                        restore_button.click()
                    self.assertEqual(restore_response.value.status, 201, restore_response.value.text)
                    receipt = restore_response.value.json()
                    restored_id = receipt['profile_id']
                    self.assertTrue(restored_id)
                    self.assertTrue(receipt['request_id'])
                    expect(page.get_by_role('status').filter(
                        has_text=f'Profile {restored_id} was restored')).to_be_visible()
                    expect(page.get_by_label('Active profile', exact=True)).to_have_value(restored_id)
                    self.assertEqual(store.profile(restored_id)['email'], source['email'])
                    self.assertTrue(all(not row['verified'] and not row['user_approved']
                                        for row in store.profile(restored_id)['facts']))

                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(page.get_by_label('Active profile', exact=True)).to_have_value(restored_id)
                    expect(page.get_by_label('Email', exact=True)).to_have_value(source['email'])
                    self.assertEqual(store.profile(restored_id)['email'], source['email'])
                    writes = [(method, path) for method, path in requests if method != 'GET']
                    self.assertEqual(writes, [
                        ('POST', '/profile-restores/preview'),
                        ('POST', '/profile-restores'),
                    ])
                    self.assertEqual(external, [])
                    self.assertEqual(errors, [], errors)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), 'Isolated profile restore API did not stop')


if __name__ == '__main__':
    unittest.main()
