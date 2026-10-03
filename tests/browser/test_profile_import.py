"""Real local browser/API acceptance for resume-first profile updates."""
from io import BytesIO
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from reportlab.pdfgen import canvas
import uvicorn

from apps.api.main import create_app
from packages.persistence.store import Store

ROOT = Path(__file__).resolve().parents[2]


def resume_pdf(extra=''):
    stream = BytesIO()
    pdf = canvas.Canvas(stream)
    text = pdf.beginText(40, 780)
    for line in ['Example Applicant', 'imported@example.invalid', '+1 202 555 0142',
                 'Skills', 'Python, SQL', 'Experience', 'Example Company | Engineer | 2020-2024', extra]:
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
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
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
                    page.get_by_role('button', name='Save profile', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()
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
                    review.get_by_label('Add contact email', exact=False).check()
                    review.get_by_label('Add contact phones', exact=False).check()
                    page.get_by_role('button', name='Add selected to profile editor', exact=True).click()
                    expect(page.get_by_label('Full name', exact=True)).to_have_value('Original Applicant')
                    expect(page.get_by_label('Email', exact=True)).to_have_value('imported@example.invalid')
                    expect(phones).to_have_value('+1 202 555 0142')
                    page.get_by_role('button', name='Save profile', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()
                    saved = store.profile(profile_id)
                    count = len(saved['facts'])
                    self.assertGreaterEqual(count, 3)
                    self.assertTrue(all(not f['verified'] and not f['user_approved'] for f in saved['facts']))
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(page.get_by_label('Email', exact=True)).to_have_value('imported@example.invalid')
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
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(page.locator('[data-resume-version="2"]')).to_be_visible()
                    self.assertEqual(page.locator('[data-resume-series]').count(), 1)
                    page.set_viewport_size({'width':390,'height':844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    self.assertEqual(store.count('applications'), 0)
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
