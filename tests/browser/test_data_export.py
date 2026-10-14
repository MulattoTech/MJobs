"""Connected local browser acceptance for profile-scoped data exports."""
import csv
import copy
import io
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
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class DataExportBrowserTests(unittest.TestCase):
    def test_all_exports_download_readably_and_remain_profile_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            first = copy.deepcopy(PROFILE)
            first.update(id='export-fixture-one', name='Export Fixture One',
                         email='export-one@example.invalid')
            first['facts'].append({
                'id': 'synthetic-one', 'text': 'fixture-one-only-value',
                'kind': 'skill', 'source': 'synthetic test fixture',
                'verified': True, 'user_approved': True, 'confidence': 100,
            })
            second = copy.deepcopy(PROFILE)
            second.update(id='export-fixture-two', name='Export Fixture Two',
                          email='export-two@example.invalid')
            second['facts'].append({
                'id': 'synthetic-two', 'text': 'fixture-two-only-value',
                'kind': 'skill', 'source': 'synthetic test fixture',
                'verified': True, 'user_approved': True, 'confidence': 100,
            })
            store.save_profile(first)
            store.save_profile(second)
            with fixture_server() as fixture:
                discover(store, fixture, 'export-fixture-one')
            job = store.jobs('export-fixture-one')[0]
            application_id = prepare(store, job['id'], 'export-fixture-one', root)
            with store.engine.begin() as connection:
                connection.execute(m.profile_versions.update().where(
                    m.profile_versions.c.profile_id == 'export-fixture-one',
                ).values(answers=[{'metadata': {
                    'api_key': 'SYNTHETIC_BROWSER_API_KEY',
                    'Authorization': 'SYNTHETIC_BROWSER_AUTH_HEADER',
                    'work_authorization': 'Synthetic employment answer',
                }}]))
                connection.execute(m.routes.update().where(
                    m.routes.c.job_id == job['id'],
                ).values(url='https://jobs.example.test/apply/%73ession/SYNTHETIC_BROWSER_PATH_TOKEN'))
            store.engine.dispose()
            app = create_app(
                store, 'http://127.0.0.1:1', root,
                'redis://127.0.0.1:1/0', operation_mode='FIXTURE',
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

                    download_count = []
                    page.on('download', lambda download: download_count.append(download.suggested_filename))

                    def download_export(profile_id, button_name, route, expected_filename):
                        with page.expect_download() as download_info:
                            with page.expect_response(
                                lambda response: response.url.endswith(
                                    f'/profiles/{profile_id}/exports/{route}')) as response_info:
                                page.get_by_role(
                                    'button', name=button_name, exact=True).click()
                        response = response_info.value
                        self.assertEqual(response.status, 200, response.status_text)
                        download = download_info.value
                        self.assertEqual(download.suggested_filename, expected_filename)
                        return Path(download.path()).read_text(encoding='utf-8')

                    first_profile_text = download_export(
                        'export-fixture-one', 'Download profile JSON', 'profile.json',
                        'mjobs-profile-export-fixture-one.json')
                    first_export = json.loads(first_profile_text)
                    self.assertEqual(first_export['profile_id'], 'export-fixture-one')
                    self.assertEqual(first_export['profile']['id'], 'export-fixture-one')
                    self.assertEqual(first_export['profile']['name'], 'Export Fixture One')
                    self.assertIn('fixture-one-only-value', json.dumps(first_export))
                    self.assertNotIn('fixture-two-only-value', json.dumps(first_export))
                    self.assertNotIn('SYNTHETIC_BROWSER_API_KEY', first_profile_text)
                    self.assertNotIn('SYNTHETIC_BROWSER_AUTH_HEADER', first_profile_text)
                    self.assertIn('Synthetic employment answer', first_profile_text)
                    expect(export_panel.get_by_role('status')).to_have_text('Export downloaded.')

                    jobs_text = download_export(
                        'export-fixture-one', 'Download jobs CSV', 'jobs.csv',
                        'mjobs-jobs-export-fixture-one.csv')
                    jobs = list(csv.DictReader(io.StringIO(jobs_text)))
                    self.assertNotIn('SYNTHETIC_BROWSER_PATH_TOKEN', jobs_text)
                    self.assertTrue(jobs, 'jobs export should include the synthetic matched posting')
                    self.assertIn(job['id'], {row['id'] for row in jobs})

                    applications_text = download_export(
                        'export-fixture-one', 'Download applications CSV', 'applications.csv',
                        'mjobs-applications-export-fixture-one.csv')
                    applications = list(csv.DictReader(io.StringIO(applications_text)))
                    self.assertNotIn('SYNTHETIC_BROWSER_PATH_TOKEN', applications_text)
                    self.assertEqual({row['id'] for row in applications}, {application_id})

                    backup_text = download_export(
                        'export-fixture-one', 'Download complete backup', 'backup.json',
                        'mjobs-backup-export-fixture-one.json')
                    first_backup = json.loads(backup_text)
                    self.assertNotIn('SYNTHETIC_BROWSER_PATH_TOKEN', backup_text)
                    self.assertTrue(all(row['url'] is None for row in first_backup['application_routes']
                                        if row['job_id'] == job['id']))
                    with store.engine.connect() as connection:
                        saved_url = connection.execute(m.routes.select().where(
                            m.routes.c.job_id == job['id'])).mappings().first()['url']
                    self.assertIn('SYNTHETIC_BROWSER_PATH_TOKEN', saved_url)
                    self.assertEqual(first_backup['export_type'], 'profile_backup')
                    self.assertEqual(first_backup['profile_id'], 'export-fixture-one')
                    self.assertNotIn('SYNTHETIC_BROWSER_API_KEY', backup_text)
                    self.assertNotIn('SYNTHETIC_BROWSER_AUTH_HEADER', backup_text)
                    self.assertIn('Synthetic employment answer', backup_text)
                    self.assertIn(job['id'], {row['id'] for row in first_backup['jobs']})
                    self.assertIn(application_id,
                                  {row['id'] for row in first_backup['applications']})

                    page.get_by_label('Active profile', exact=True).select_option('export-fixture-two')
                    expect(export_panel).to_be_visible()
                    second_profile_text = download_export(
                        'export-fixture-two', 'Download profile JSON', 'profile.json',
                        'mjobs-profile-export-fixture-two.json')
                    second_export = json.loads(second_profile_text)
                    self.assertEqual(second_export['profile_id'], 'export-fixture-two')
                    self.assertEqual(second_export['profile']['name'], 'Export Fixture Two')
                    self.assertIn('fixture-two-only-value', json.dumps(second_export))
                    self.assertNotIn('fixture-one-only-value', json.dumps(second_export))

                    second_backup_text = download_export(
                        'export-fixture-two', 'Download complete backup', 'backup.json',
                        'mjobs-backup-export-fixture-two.json')
                    second_backup = json.loads(second_backup_text)
                    self.assertEqual(second_backup['profile_id'], 'export-fixture-two')
                    self.assertIn('fixture-two-only-value', json.dumps(second_backup))
                    self.assertNotIn('fixture-one-only-value', json.dumps(second_backup))
                    self.assertNotIn(application_id,
                                     {row['id'] for row in second_backup['applications']})

                    for button_name, route in (
                        ('Download jobs CSV', 'jobs.csv'),
                        ('Download applications CSV', 'applications.csv'),
                    ):
                        with page.expect_response(lambda response, route=route: response.url.endswith(
                                f'/profiles/export-fixture-two/exports/{route}')) as empty_response:
                            page.get_by_role('button', name=button_name, exact=True).click()
                        self.assertEqual(empty_response.value.status, 200)
                        expect(export_panel.get_by_role('status')).to_have_text(
                            'There is no data in this category to export. No file was downloaded.')
                    self.assertEqual(len(download_count), 6, download_count)

                    export_requests = [path for method, path in requests if '/exports/' in path]
                    self.assertEqual(export_requests, [
                        '/profiles/export-fixture-one/exports/profile.json',
                        '/profiles/export-fixture-one/exports/jobs.csv',
                        '/profiles/export-fixture-one/exports/applications.csv',
                        '/profiles/export-fixture-one/exports/backup.json',
                        '/profiles/export-fixture-two/exports/profile.json',
                        '/profiles/export-fixture-two/exports/backup.json',
                        '/profiles/export-fixture-two/exports/jobs.csv',
                        '/profiles/export-fixture-two/exports/applications.csv',
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

