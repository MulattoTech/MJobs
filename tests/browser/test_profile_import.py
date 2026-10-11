"""Real local browser/API acceptance for resume-first profile updates."""
from io import BytesIO
from pathlib import Path
from contextlib import contextmanager
import os
from fixtures.browser_ports import bound_browser_socket
from fixtures.resume_scans import scanned_resume
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from reportlab.pdfgen import canvas
import uvicorn

from apps.api.main import create_app
from packages.persistence.store import Store
from services.career_profile import ocr
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def dispatch_queued_resume_extractions(store, output_dir, operation_mode="PERSONAL", worker_gate=None):
    """Run the real worker locally for this browser test; no Redis service needed."""
    from apps.api import main as api_main
    from workers.tasks import execute_operation
    real_queue = api_main.queue_durable_extraction
    threads, failures = [], []
    worker_env = {
        "MJOBS_DATABASE_URL": store.engine.url.render_as_string(hide_password=False),
        "MJOBS_OPERATION_MODE": operation_mode,
        "MJOBS_OUTPUT_DIR": str(output_dir),
    }

    def admit_then_start(*args, **kwargs):
        queued = real_queue(*args, **kwargs)
        def run():
            try:
                if worker_gate is not None and not worker_gate.wait(15):
                    raise TimeoutError("browser test did not release queued extraction worker")
                with patch.dict(os.environ, worker_env):
                    execute_operation(queued["operation_id"])
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=run, daemon=True)
        threads.append(thread)
        thread.start()
        return queued

    with patch.object(api_main, "queue_durable_extraction", side_effect=admit_then_start):
        yield
    for thread in threads:
        thread.join(timeout=15)
        if thread.is_alive():
            raise AssertionError("queued resume extraction worker did not settle")
    if failures:
        raise failures[0]


def resume_pdf(extra=''):
    stream = BytesIO()
    pdf = canvas.Canvas(stream)
    text = pdf.beginText(40, 780)
    for line in ['Example Applicant', 'imported@example.invalid', '+1 202 555 0142',
                 'GitHub: github.com/imported-example', 'Portfolio: https://portfolio.example.invalid',
                 'Skills', 'Python, SQL', 'Work History', 'Example Company | Engineer | 2020-2024',
                 'Responsibilities', 'Maintained support@example.invalid routing.',
                 'Technology Experience', 'Linux services', extra]:
        text.textLine(line)
    pdf.drawText(text)
    pdf.save()
    return stream.getvalue()


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class ProfileImportBrowserTests(unittest.TestCase):
    def test_browser_cancel_stops_resume_extract_without_profile_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser-cancel.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            store.save_profile(PROFILE)
            uploaded = save_resume(store, root, PROFILE['id'], 'base.pdf', 'application/pdf', resume_pdf())
            before = store.profile(PROFILE['id'])
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='PERSONAL')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
            started, cancelled = threading.Event(), threading.Event()

            def block_until_cancelled(data, format, source, cancellation_event):
                started.set()
                deadline = time.monotonic() + 5
                from workers.tasks import OperationCancelled
                try:
                    while not cancellation_event.is_set() and time.monotonic() < deadline:
                        time.sleep(0.005)
                except OperationCancelled:
                    cancelled.set()
                    raise
                raise AssertionError('explicit browser cancellation did not reach extraction work')

            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated browser cancellation API did not start')
                    time.sleep(0.02)
                from services.career_profile import durable_extraction
                with patch.object(durable_extraction, 'extract_resume', side_effect=block_until_cancelled):
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page()
                        page.goto(origin, wait_until='domcontentloaded')
                        page.get_by_role('button', name='Profile', exact=True).click()
                        page.locator('#active-profile').select_option(PROFILE['id'])
                        row = page.locator('[data-resume-version="1"]')
                        expect(row).to_be_visible()
                        with dispatch_queued_resume_extractions(store, root):
                            row.get_by_role('button', name='Extract and review').click()
                            self.assertTrue(started.wait(5), 'browser extraction request did not start')
                            row.get_by_role('button', name='Cancel extraction', exact=True).click()
                            expect(page.get_by_role('status').filter(has_text='cancellation requested')).to_be_visible()
                            self.assertTrue(cancelled.wait(5), 'browser cancel control did not stop extraction work')
                            self.assertEqual(store.profile(PROFILE['id']), before)
                            self.assertEqual(store.count('applications'), 0)
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_import_review_preferences_and_resume_history_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='PERSONAL')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated browser API did not start')
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(viewport={'width':1365,'height':1000})
                    external, errors = [], []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    page.get_by_label('Full name', exact=True).fill('Original Applicant')
                    page.get_by_label('Email', exact=True).fill('original@example.invalid')
                    phones = page.get_by_label('Phone numbers (one per line)')
                    phones.fill('+1 202 555 0100')
                    phones.press('End')
                    phones.press('Enter')
                    phones.press_sequentially('+1 202 555 0101')
                    expect(phones).to_have_value('+1 202 555 0100\n+1 202 555 0101')
                    page.get_by_label('Desired roles (one per line)').fill('Engineer\nConsultant')
                    page.get_by_label('Work authorization details').fill('Authorized in the United States')
                    page.get_by_label('Sponsorship needs').select_option('NOT_REQUIRED')
                    contacts = {'first name':'Original', 'middle name':'Q', 'last name':'Applicant',
                                'preferred name':'Ori', 'city':'Tulsa', 'state':'OK', 'postal code':'74101',
                                'country':'US', 'linkedin':'https://linkedin.com/in/example',
                                'github':'https://github.com/example', 'portfolio':'https://example.invalid'}
                    for label, value in contacts.items():
                        page.get_by_label(label, exact=True).fill(value)
                    page.get_by_role('button', name='Save profile', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()
                    # Saving persists first; the dropdown options arrive via the
                    # subsequent profile-list readback. Wait on that actual state.
                    expect(page.locator('#active-profile')).not_to_have_value('')
                    profile_id = page.locator('#active-profile').input_value()
                    self.assertEqual(store.profile(profile_id)['facts'], [])
                    self.assertEqual(store.profile(profile_id)['preferences']['desired_roles'], ['Engineer','Consultant'])
                    page.get_by_label('Choose a resume (up to 10 MiB)').set_input_files(
                        {'name':'base.pdf','mimeType':'application/pdf','buffer':resume_pdf()})
                    page.get_by_label('Resume label', exact=True).fill('Engineering')
                    page.get_by_role('button', name='Store resume', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Resume stored locally')).to_be_visible()
                    worker_gate = threading.Event()
                    with dispatch_queued_resume_extractions(store, root, worker_gate=worker_gate):
                        page.get_by_role('button', name='Extract and review', exact=True).click()
                        deadline = time.monotonic() + 5
                        while store.count('worker_operations') < 1 and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertEqual(store.count('worker_operations'), 1)
                        page.reload(wait_until='domcontentloaded')
                        page.get_by_role('button', name='Profile', exact=True).click()
                        row = page.locator('[data-resume-version="1"]')
                        expect(row.get_by_role('button', name='Cancel extraction', exact=True)).to_be_visible()
                        self.assertEqual(store.count('worker_operations'), 1)
                        worker_gate.set()
                        review = page.get_by_role('region', name='Resume extraction review')
                        expect(review).to_be_visible()
                        self.assertTrue(all(not box.is_checked() for box in review.locator('input[type=checkbox]').all()))
                    expect(review.get_by_label('Add contact email', exact=False)).not_to_be_checked()
                    expect(review.get_by_label('Add contact github', exact=False)).not_to_be_checked()
                    expect(review.get_by_text('Maintained support@example.invalid routing.', exact=False)).to_be_visible()
                    expect(review.get_by_text('Dates to review: 2020 to 2024.', exact=False)).to_be_visible()
                    expect(review.get_by_text('responsibility', exact=True)).to_be_visible()
                    career_suggestions = review.locator('label.fact-suggestion:has(strong) input[type="checkbox"]')
                    self.assertGreater(career_suggestions.count(), 0)
                    for index in range(career_suggestions.count()):
                        self.assertFalse(career_suggestions.nth(index).is_checked())
                        career_suggestions.nth(index).check()
                    review.get_by_label('Add contact email', exact=False).check()
                    review.get_by_label('Add contact phones', exact=False).check()
                    review.get_by_label('Add contact github', exact=False).check()
                    page.get_by_role('button', name='Add selected to profile editor', exact=True).click()
                    expect(page.get_by_label('Full name', exact=True)).to_have_value('Original Applicant')
                    expect(page.get_by_label('Email', exact=True)).to_have_value('imported@example.invalid')
                    expect(page.get_by_label('Sponsorship needs')).to_have_value('NOT_REQUIRED')
                    expect(page.get_by_label('Work authorization details')).to_have_value('Authorized in the United States')
                    expect(phones).to_have_value('+1 202 555 0142')
                    contacts['github'] = 'https://github.com/imported-example'
                    expect(page.get_by_label('github', exact=True)).to_have_value(contacts['github'])
                    expect(page.get_by_label('portfolio', exact=True)).to_have_value(contacts['portfolio'])
                    page.get_by_role('button', name='Save profile', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()
                    saved = store.profile(profile_id)
                    count = len(saved['facts'])
                    self.assertGreaterEqual(count, 3)
                    self.assertEqual(saved['preferences']['work_authorization'], 'Authorized in the United States')
                    self.assertEqual(saved['preferences']['sponsorship_needs'], 'NOT_REQUIRED')
                    self.assertEqual(saved['answers'], [])
                    for label, value in contacts.items():
                        self.assertEqual(saved['contact'][label.replace(' ', '_')], value)
                    self.assertTrue(all(not f['verified'] and not f['user_approved'] for f in saved['facts']))
                    self.assertEqual(next(f for f in saved['facts'] if f['kind']=='responsibility')['text'],
                                     'Maintained support@example.invalid routing.')
                    self.assertIn('Example Company | Engineer | 2020-2024', [f['text'] for f in saved['facts']])
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(page.get_by_label('Email', exact=True)).to_have_value('imported@example.invalid')
                    expect(page.get_by_label('Sponsorship needs')).to_have_value('NOT_REQUIRED')
                    expect(page.get_by_label('Work authorization details')).to_have_value('Authorized in the United States')
                    with dispatch_queued_resume_extractions(store, root):
                        page.get_by_role('button', name='Extract and review', exact=True).click()
                        expect(review).to_be_visible()
                        repeated_suggestions = review.locator(
                            'label.fact-suggestion:has(strong) input[type="checkbox"]')
                        for index in range(repeated_suggestions.count()):
                            repeated_suggestions.nth(index).check()
                    page.get_by_role('button', name='Add selected to profile editor', exact=True).click()
                    page.get_by_role('button', name='Save profile', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()
                    self.assertEqual(len(store.profile(profile_id)['facts']), count)
                    page.get_by_label('Resume collection', exact=True).select_option(label='Engineering')
                    page.get_by_label('Choose a resume (up to 10 MiB)').set_input_files(
                        {'name':'updated.pdf','mimeType':'application/pdf','buffer':resume_pdf('Built documented systems.')})
                    page.get_by_role('button', name='Store resume', exact=True).click()
                    expect(page.locator('[data-resume-version="2"]')).to_be_visible()
                    expect(page.locator('[data-resume-version="1"]')).to_be_visible()
                    page.get_by_label('Choose a resume (up to 10 MiB)').set_input_files(
                        {'name':'scanned.pdf','mimeType':'application/pdf','buffer':scanned_resume(mixed=True)})
                    page.get_by_role('button', name='Store resume', exact=True).click()
                    scanned = page.locator('[data-resume-version="3"]')
                    expect(scanned).to_be_visible()
                    profile_before_failed_ocr = store.profile(profile_id)
                    with patch.object(ocr, 'RESOURCE_LIMITS_AVAILABLE', True), \
                         patch.object(ocr.shutil, 'which', side_effect=lambda name:'/synthetic/'+name), \
                         patch.object(ocr, '_run_tool', side_effect=ocr.OCRToolFailure(7, 64, False)):
                        with dispatch_queued_resume_extractions(store, root):
                            scanned.get_by_role('button', name='Extract and review', exact=True).click()
                            review = page.get_by_role('region', name='Resume extraction review')
                            expect(review.get_by_text('Extraction review: READY FOR REVIEW', exact=True)).to_be_visible()
                            expect(review.get_by_text('OCR tool exited with status 7', exact=False)).to_be_visible()
                            expect(review.get_by_text('Diagnostic output was captured and omitted for privacy.', exact=False)).to_be_visible()
                    expect(review).to_be_visible()
                    self.assertEqual(store.profile(profile_id), profile_before_failed_ocr)
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(page.locator('[data-resume-version="2"]')).to_be_visible()
                    self.assertEqual(page.locator('[data-resume-series]').count(), 1)
                    page.set_viewport_size({'width':390,'height':844})
                    # The viewport protocol returns before Chromium's responsive
                    # layout has settled. Assert the actual eventual geometry.
                    expect(page.locator('html')).to_have_js_property('scrollWidth', 390)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    self.assertEqual(store.count('applications'), 0)
                    page.get_by_role('button', name='Add fact', exact=True).click()
                    technology = page.locator('fieldset.fact').last
                    technology.get_by_role('combobox').select_option('technology_experience')
                    technology.get_by_label('Fact text', exact=True).fill('Maintained Linux services for two years')
                    technology.get_by_label('Evidence / source', exact=True).fill('User supplied career history')
                    page.get_by_role('button', name='Save profile', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    fact = next(f for f in store.profile(profile_id)['facts'] if f['text'] == 'Maintained Linux services for two years')
                    self.assertEqual(fact['kind'], 'technology_experience')
                    self.assertEqual(fact['text'], 'Maintained Linux services for two years')
                    self.assertFalse(fact['verified'])
                    self.assertFalse(fact['user_approved'])
                    page.get_by_role('button', name='New profile', exact=True).click()
                    expect(page.get_by_label('Full name', exact=True)).to_have_value('')
                    expect(page.locator('#active-profile')).to_have_value('')
                    expect(page.get_by_label('Choose a resume (up to 10 MiB)')).to_have_count(0)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
