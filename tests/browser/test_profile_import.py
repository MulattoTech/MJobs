"""Real local browser/API acceptance for resume-first profile updates."""
from io import BytesIO
from pathlib import Path
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

ROOT = Path(__file__).resolve().parents[2]


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
                    page.get_by_role('button', name='Extract and review', exact=True).click()
                    review = page.get_by_role('region', name='Resume extraction review')
                    expect(review).to_be_visible()
                    expect(review.get_by_label('Add contact email', exact=False)).not_to_be_checked()
                    expect(review.get_by_label('Add contact github', exact=False)).not_to_be_checked()
                    expect(review.get_by_text('Maintained support@example.invalid routing.', exact=False)).to_be_visible()
                    expect(review.get_by_text('Dates to review: 2020 to 2024.', exact=False)).to_be_visible()
                    expect(review.get_by_text('responsibility', exact=True)).to_be_visible()
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
                    page.get_by_role('button', name='Extract and review', exact=True).click()
                    expect(review).to_be_visible()
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
