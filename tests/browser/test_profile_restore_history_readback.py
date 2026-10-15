"""Durable archive readback after real backup import, reload and read failures."""
import copy
import json
import re
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import select
from packages.persistence import models as m
from tests.integration import test_profile_restore as restore_fixture

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from services.data_export import export_profile_backup
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class ProfileRestoreHistoryReadbackBrowserTests(unittest.TestCase):
    def test_archive_reload_retry_and_profile_response_isolation(self):
        with tempfile.TemporaryDirectory(prefix='mjobs-profile-restore-browser-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            source = copy.deepcopy(PROFILE)
            source.update(id='restore-browser-source', name='Synthetic Restore Source',
                          email='restore-source@example.invalid')
            store.save_profile(source)
            # Reuse the real complete-backup producer and synthetic safe-graph fixture.
            producer = restore_fixture.ProfileRestoreIntegrationTests()
            self.addCleanup(producer.doCleanups)
            producer.setUp()
            backup = producer.backup
            source_before = export_profile_backup(producer.store, producer.source_profile_id,
                                                  output_dir=producer.root)
            target_before = store.profile(source['id'])
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
                    page.set_default_timeout(8000)
                    page.add_init_script(
                        "if (!localStorage.getItem('mjobs-profile')) "
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
                    expect(preview).to_contain_text('Synthetic Source')
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
                    self.assertEqual(store.profile(restored_id)['email'], 'source@example.invalid')
                    self.assertTrue(all(not row['verified'] and not row['user_approved']
                                        for row in store.profile(restored_id)['facts']))

                    history = page.get_by_role('region', name='Recovered company discovery history', exact=True)
                    expect(history).to_contain_text('HIGH_MATCH_JOB')
                    expect(history).to_contain_text('read-only historical snapshots')
                    durable = page.request.get(origin + f'/profile-restores/{restored_id}/history').json()
                    self.assertEqual(durable['mode'], 'READ_ONLY_HISTORICAL_ARCHIVE')
                    self.assertTrue(durable['authorization_required_for_future_live_action'])
                    self.assertEqual(len(durable['history']), 1)
                    with store.engine.connect() as connection:
                        inert_before = {table.name: connection.execute(select(table)).all()
                                        for table in (m.jobs, m.applications, m.profile_recovery_history_snapshots)}

                    # Reload must reconstruct the archive from durable API state.
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(page.get_by_label('Active profile', exact=True)).to_have_value(restored_id)
                    expect(history).to_contain_text('HIGH_MATCH_JOB')
                    # Tab navigation unmounts ProfileRestore independently of a page reload.
                    page.get_by_role('button', name=re.compile(r'^Jobs\b')).click()
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(history).to_contain_text('HIGH_MATCH_JOB')

                    history_url = f'**/profile-restores/{restored_id}/history'
                    def fail_read(route):
                        route.fulfill(status=503, json={'detail': 'PRIVATE_SYNTHETIC_ERROR'})
                    page.route(history_url, fail_read)
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(history.get_by_role('alert')).to_contain_text('could not be loaded')
                    expect(history).not_to_contain_text('HIGH_MATCH_JOB')
                    expect(history).not_to_contain_text('PRIVATE_SYNTHETIC_ERROR')
                    page.unroute(history_url, fail_read)
                    history.get_by_role('button', name='Retry recovered history', exact=True).click()
                    expect(history).to_contain_text('HIGH_MATCH_JOB')

                    pending = []
                    def delay_read(route):
                        pending.append((route, route.fetch()))
                    page.route(history_url, delay_read)
                    page.get_by_role('button', name=re.compile(r'^Jobs\b')).click()
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(history).to_contain_text('Loading recovered history')
                    # Wait on the actual intercepted read, not a timing assumption.
                    deadline = time.monotonic() + 10
                    while not pending and time.monotonic() < deadline:
                        page.wait_for_timeout(20)
                    self.assertEqual(len(pending), 1)
                    page.get_by_label('Active profile', exact=True).select_option(source['id'])
                    expect(history).to_contain_text('No recovered company discovery history')
                    route, response = pending.pop()
                    route.fulfill(response=response)
                    # Flush fetch completion and a browser presentation cycle before asserting isolation.
                    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    # A later empty read and subsequent render must not reveal the old result.
                    page.unroute(history_url, delay_read)
                    expect(history).not_to_contain_text('HIGH_MATCH_JOB')
                    expect(history).to_contain_text('No recovered company discovery history')
                    page.get_by_label('Active profile', exact=True).select_option(restored_id)
                    expect(history).to_contain_text('HIGH_MATCH_JOB')

                    # Late reads after unmount cannot populate a fresh component instance.
                    page.route(history_url, delay_read)
                    page.get_by_role('button', name=re.compile(r'^Jobs\b')).click()
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(history).to_contain_text('Loading recovered history')
                    deadline = time.monotonic() + 10
                    while not pending and time.monotonic() < deadline:
                        page.wait_for_timeout(20)
                    self.assertEqual(len(pending), 1)
                    page.get_by_role('button', name=re.compile(r'^Jobs\b')).click()
                    page.get_by_label('Active profile', exact=True).select_option(source['id'])
                    route, response = pending.pop()
                    route.fulfill(response=response)
                    # Flush fetch completion and a browser presentation cycle before asserting isolation.
                    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    page.unroute(history_url, delay_read)
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(history).to_contain_text('No recovered company discovery history')
                    expect(history).not_to_contain_text('HIGH_MATCH_JOB')

                    self.assertEqual(store.profile(source['id']), target_before)
                    self.assertEqual(export_profile_backup(producer.store, producer.source_profile_id,
                                                           output_dir=producer.root), source_before)
                    with store.engine.connect() as connection:
                        for table in (m.jobs, m.applications, m.profile_recovery_history_snapshots):
                            self.assertEqual(connection.execute(select(table)).all(), inert_before[table.name])
                        for table in (m.company_sources, m.company_discovery_requests, m.operations):
                            self.assertEqual(connection.execute(select(table)).all(), [])
                    self.assertEqual(page.request.get(origin + f'/profile-restores/{restored_id}/history').json(), durable)
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
