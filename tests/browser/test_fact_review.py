"""Synthetic Chromium/API/worker proof that content edits require fresh review."""
import asyncio
import copy
import hashlib
from io import BytesIO
import re
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from docx import Document
from pypdf import PdfReader
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class FactReviewBrowserTests(unittest.TestCase):
    def test_text_edit_requires_fresh_review(self):
        self.exercise('text', 'Reviewed synthetic Python experience')

    def test_source_edit_requires_fresh_review(self):
        self.exercise('source', 'Replacement synthetic evidence')

    def test_kind_edit_requires_fresh_review(self):
        self.exercise('kind', 'technology_experience')

    def test_demo_profile_stays_read_only_until_saved_version_loads(self):
        with tempfile.TemporaryDirectory(prefix='mjobs-demo-profile-load-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'test.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            profile_read_started = threading.Event()
            release_profile_read = threading.Event()

            async def delay_demo_profile_read(request, call_next):
                if request.method == 'GET' and request.url.path.startswith('/profiles/demo-'):
                    profile_read_started.set()
                    while not release_profile_read.is_set():
                        await asyncio.sleep(.01)
                return await call_next(request)

            app.middleware('http')(delay_demo_profile_read)
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Demo-profile isolated API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)

                    context.route('**/discovery', lambda route: route.fulfill(
                        status=202, content_type='application/json', body='{"operation_id":"demo-op"}'))
                    context.route('**/operations/demo-op', lambda route: route.fulfill(
                        status=200, content_type='application/json',
                        body='{"id":"demo-op","kind":"DISCOVERY","state":"COMPLETED","error":null}'))
                    page = context.new_page()
                    page.goto(origin)
                    page.get_by_role('button', name='Load fictional demo', exact=True).first.click()
                    deadline = time.monotonic() + 5
                    while not profile_read_started.is_set() and time.monotonic() < deadline:
                        page.wait_for_timeout(25)
                    self.assertTrue(profile_read_started.is_set(), 'selected profile read did not start')
                    expect(page.get_by_role('status').filter(has_text='Fictional demo loaded.')).to_be_visible()
                    page.get_by_role('button', name='Profile', exact=True).click()
                    save = page.get_by_role('button', name='Save profile and answers', exact=True)
                    expect(page.get_by_role('status').filter(
                        has_text='Loading the selected profile before edits can be made.')).to_be_visible(timeout=1000)
                    expect(save).to_be_disabled()
                    expect(page.get_by_role('textbox', name='Full name', exact=True)).to_be_disabled()
                    release_profile_read.set()
                    expect(save).to_be_enabled(timeout=5000)
                    expect(page.get_by_role('textbox', name='Full name', exact=True)).to_have_value('Fictional Test Applicant')
                    demo_id = next(p['id'] for p in store.profiles() if p['name'] == 'Fictional Test Applicant')
                    self.assertTrue(store.profile(demo_id)['version'])
                    context.close()
                    browser.close()
            finally:
                release_profile_read.set()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def exercise(self, field, value):
        with tempfile.TemporaryDirectory(prefix='mjobs-fact-review-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'test.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            profile = copy.deepcopy(PROFILE)
            profile['facts'] = profile['facts'][:2]
            store.save_profile(profile)
            target_id = profile['facts'][0]['id']
            initial_profile = copy.deepcopy(store.profile('fixture'))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            job = store.jobs()[0]
            application_id = prepare(store, job['id'], 'fixture', root)
            historical = copy.deepcopy(application_documents(store, application_id))
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Fact-review isolated API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    external = []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")

                    def run_operation(operation_id):
                        with patch.dict('os.environ', MJOBS_DATABASE_URL=store.url,
                                        MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',
                                        MJOBS_OUTPUT_DIR=str(root), MJOBS_OPERATION_MODE='FIXTURE'):
                            self.assertEqual(execute_operation(operation_id)['status'], 'COMPLETED')

                    def open_profile():
                        page.reload()
                        page.get_by_role('button', name='Profile', exact=True).click()
                        row = page.get_by_role('group', name=re.compile(r'^Fact 1 (?:Eligible|Pending)$'))
                        expect(row.get_by_role('textbox', name='Fact text', exact=True)).to_be_visible()
                        return row

                    def save_profile():
                        # The unchanged second approved fact keeps normal matching enabled.
                        with page.expect_response(lambda r: r.url == origin+'/matches' and r.request.method == 'POST') as response:
                            page.get_by_role('button', name='Save profile', exact=True).click()
                        run_operation(response.value.json()['operation_id'])
                        expect(page.get_by_role('button', name='Save profile', exact=True)).to_be_enabled()
                        expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()

                    def saved_fact():
                        current = store.profile('fixture')
                        self.assertNotEqual(current['version'], initial_profile['version'])
                        self.assertEqual({f.get('fact_id', f.get('id')) for f in current['facts']},
                                         {f.get('fact_id', f.get('id')) for f in initial_profile['facts']})
                        return next(f for f in current['facts'] if f.get('fact_id', f.get('id')) == target_id)

                    def prepare_from_ui(expected_eligible):
                        page.get_by_role('navigation').get_by_role('button', name='Jobs', exact=False).click()
                        page.get_by_role('button').filter(has=page.get_by_role('heading', name=job['title'], exact=True)).click()
                        with page.expect_response(lambda r: r.url == origin+'/applications' and r.request.method == 'POST') as response:
                            page.get_by_role('button', name='Prepare documents', exact=True).click()
                        operation = response.value.json()
                        run_operation(operation['operation_id'])
                        rows = application_documents(store, operation['application_id'])
                        latest = max((r for r in rows if r['kind'] == 'resume'), key=lambda r: r['version'])
                        self.assertEqual(target_id in latest['fact_ids'], expected_eligible)
                        document = page.get_by_role('region', name=f"resume version {latest['version']}", exact=True)
                        target_text = value if field == 'text' else profile['facts'][0]['text']
                        for fmt in ('txt', 'docx', 'pdf'):
                            with page.expect_download() as event:
                                document.get_by_role('link', name='Download '+fmt.upper(), exact=True).click()
                            downloaded = Path(event.value.path()).read_bytes()
                            self.assertEqual(hashlib.sha256(downloaded).hexdigest(), latest['sha256'][fmt])
                            if fmt == 'txt':
                                text = downloaded.decode('utf-8')
                            elif fmt == 'docx':
                                text = '\n'.join(p.text for p in Document(BytesIO(downloaded)).paragraphs)
                            else:
                                text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(downloaded)).pages)
                            self.assertEqual(target_text in text, expected_eligible, fmt)
                            if not expected_eligible:
                                self.assertNotIn(profile['facts'][0]['text'], text, fmt)
                            self.assertIn(profile['facts'][1]['text'], text, fmt)

                    row = open_profile()
                    expect(row).to_have_accessible_name('Fact 1 Eligible')
                    row.get_by_role('spinbutton', name='Confidence (0–100)', exact=True).fill('95')
                    expect(row.get_by_role('checkbox', name='I verified this fact', exact=True)).to_be_checked()
                    expect(row.get_by_role('checkbox', name='I approve its use in documents', exact=True)).to_be_checked()
                    if field == 'kind':
                        row.get_by_role('combobox', name='Type', exact=True).select_option(value)
                    else:
                        row.get_by_role('textbox', name='Fact text' if field == 'text' else 'Evidence / source', exact=True).fill(value)
                    expect(row, 'REVIEW_RESET_REQUIRED').to_have_accessible_name('Fact 1 Pending')
                    expect(row.get_by_role('checkbox', name='I verified this fact', exact=True)).not_to_be_checked()
                    expect(row.get_by_role('checkbox', name='I approve its use in documents', exact=True)).not_to_be_checked()
                    save_profile()
                    self.assertFalse(saved_fact()['verified'])
                    self.assertFalse(saved_fact()['user_approved'])
                    self.assertEqual(saved_fact()[field], value)
                    row = open_profile()
                    expect(row).to_have_accessible_name('Fact 1 Pending')
                    prepare_from_ui(False)

                    row = open_profile()
                    row.get_by_role('checkbox', name='I verified this fact', exact=True).check()
                    expect(row).to_have_accessible_name('Fact 1 Pending')
                    save_profile()
                    self.assertTrue(saved_fact()['verified'])
                    self.assertFalse(saved_fact()['user_approved'])
                    prepare_from_ui(False)

                    row = open_profile()
                    row.get_by_role('checkbox', name='I approve its use in documents', exact=True).check()
                    expect(row).to_have_accessible_name('Fact 1 Eligible')
                    save_profile()
                    self.assertTrue(saved_fact()['verified'])
                    self.assertTrue(saved_fact()['user_approved'])
                    prepare_from_ui(True)

                    # Explicit API flags remain meaningful; this is not a server-wide policy change.
                    api_profile = copy.deepcopy(profile)
                    api_profile['facts'][0]['text'] = 'Explicitly API-approved synthetic fact'
                    api_profile['expected_version'] = store.profile('fixture')['version']
                    response = context.request.put(origin+'/profiles', data=api_profile)
                    self.assertEqual(response.status, 200)
                    self.assertTrue(saved_fact()['verified'])
                    self.assertTrue(saved_fact()['user_approved'])
                    self.assertEqual(saved_fact()['text'], api_profile['facts'][0]['text'])
                    current_rows = {r['id']: r for r in application_documents(store, application_id)}
                    for old_row in historical:
                        self.assertEqual(current_rows[old_row['id']], old_row)
                    for old_row in historical:
                        original = page.get_by_role('region', name=f"{old_row['kind']} version {old_row['version']}", exact=True)
                        for fmt in ('pdf', 'docx', 'txt'):
                            with page.expect_download() as event:
                                original.get_by_role('link', name='Download '+fmt.upper(), exact=True).click()
                            self.assertEqual(hashlib.sha256(Path(event.value.path()).read_bytes()).hexdigest(), old_row['sha256'][fmt])
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
