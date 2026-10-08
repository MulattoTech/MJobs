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
    def test_cover_letter_comparison_uses_only_validated_same_application_previews(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'comparison.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE, facts=[PROFILE['facts'][0]]))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            jobs = store.jobs()
            first_job = next(row for row in jobs if row['requisition'] == 'B')
            application_id = prepare(store, first_job['id'], 'fixture', root)
            first_rows = application_documents(store, application_id, root)
            first_letter = next(row for row in first_rows if row['kind'] == 'cover-letter')
            first_text = (root / application_id / first_letter['files']['txt']).read_bytes()
            prepare(store, first_job['id'], 'fixture', root, cover_letter_style='warm', cover_letter_length='detailed')
            revisions = [row for row in application_documents(store, application_id, root) if row['kind'] == 'cover-letter']
            second_letter = next(row for row in revisions if row['version'] == 2)
            second_path = root / application_id / second_letter['files']['txt']
            other_job = next(row for row in jobs if row['id'] != first_job['id'])
            other_application_id = prepare(store, other_job['id'], 'fixture', root)
            other_letter = next(row for row in application_documents(store, other_application_id, root)
                                if row['kind'] == 'cover-letter')
            prepare(store, other_job['id'], 'fixture', root, cover_letter_style='warm', cover_letter_length='detailed')
            other_revisions = [row for row in application_documents(store, other_application_id, root)
                               if row['kind'] == 'cover-letter']
            other_second_letter = next(row for row in other_revisions if row['version'] == 2)
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
                        self.fail('Document comparison API did not start')
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
                    page=context.new_page();page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload();page.get_by_role('button',name='Applications',exact=False).first.click()
                    page.locator('.job-card').filter(has_text=first_job['title']).first.click()
                    earlier=page.get_by_label('Earlier cover-letter version',exact=True)
                    later=page.get_by_label('Later cover-letter version',exact=True)
                    expect(earlier.locator('option')).to_have_count(2)
                    self.assertEqual({option.get_attribute('value') for option in earlier.locator('option').all()},
                                     {first_letter['id'],second_letter['id']})
                    self.assertNotIn(other_letter['id'], {option.get_attribute('value') for option in earlier.locator('option').all()})
                    earlier.select_option(first_letter['id']);later.select_option(second_letter['id'])
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    result=page.get_by_role('region',name='Cover-letter comparison result',exact=True)
                    expect(result.get_by_text('Cover letter · v1',exact=True)).to_be_visible()
                    expect(result.get_by_text('Cover letter · v2',exact=True)).to_be_visible()
                    expect(result).to_contain_text(first_letter['preview_text'])
                    expect(result).to_contain_text(second_letter['preview_text'])
                    with page.expect_download() as event:
                        page.get_by_role('region',name='cover-letter version 1',exact=True).get_by_role('link',name='Download TXT',exact=True).click()
                    self.assertEqual(Path(event.value.path()).read_bytes(),first_text)

                    page.locator('.job-card').filter(has_text=other_job['title']).first.click()
                    other_result=page.get_by_role('region',name='Cover-letter comparison result',exact=True)
                    expect(other_result).to_have_count(0)
                    self.assertNotIn(first_letter['preview_text'],page.locator('body').inner_text())
                    expect(page.get_by_label('Earlier cover-letter version',exact=True).locator('option')).to_have_count(2)
                    page.get_by_label('Earlier cover-letter version',exact=True).select_option(other_letter['id'])
                    page.get_by_label('Later cover-letter version',exact=True).select_option(other_second_letter['id'])
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    expect(other_result).to_contain_text(other_letter['preview_text'])
                    expect(other_result).to_contain_text(other_second_letter['preview_text'])
                    page.locator('.job-card').filter(has_text=first_job['title']).first.click()
                    result=page.get_by_role('region',name='Cover-letter comparison result',exact=True)
                    expect(result).to_have_count(0)
                    self.assertNotIn(other_letter['preview_text'],page.locator('body').inner_text())
                    page.get_by_label('Earlier cover-letter version',exact=True).select_option(first_letter['id'])
                    page.get_by_label('Later cover-letter version',exact=True).select_option(second_letter['id'])
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    expect(result).to_contain_text(first_letter['preview_text'])

                    current_profile=store.profile('fixture')
                    current_profile['preferences']={**current_profile.get('preferences',{}),'avoid_terms':['Application']}
                    store.save_profile(current_profile)
                    validated_history=context.request.get(f'{origin}/profiles/fixture/applications/{application_id}/documents').json()['documents']
                    self.assertTrue(next(row for row in validated_history if row['id']==second_letter['id'])['avoid_list_error'])
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    expect(page.get_by_role('alert')).to_contain_text('Cover-letter validation failed')
                    expect(result).to_have_count(0)
                    current_profile=store.profile('fixture')
                    current_profile['preferences']={**current_profile.get('preferences',{}),'avoid_terms':[]}
                    store.save_profile(current_profile)
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    expect(result).to_contain_text(first_letter['preview_text'])

                    endpoint=f'**/profiles/fixture/applications/{application_id}/documents'
                    page.route(endpoint,lambda route: route.fulfill(status=503,content_type='application/json',body='{"detail":"history read unavailable"}'))
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    expect(page.get_by_role('alert')).to_contain_text('history read unavailable')
                    page.unroute(endpoint)

                    second_path.write_text(second_letter['preview_text']+'\nCorrupted text.\n',encoding='utf-8')
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    expect(page.get_by_role('alert')).to_contain_text('Cover-letter validation failed')
                    second_path.unlink()
                    page.get_by_role('button',name='Compare selected cover letters',exact=True).click()
                    expect(page.get_by_role('alert')).to_contain_text('Cover-letter validation failed')
                    self.assertEqual(store.application(application_id)['document_version'],2)
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

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
                        expect(document.get_by_text('Generation method: deterministic-fact-copy-v2',exact=True)).to_be_visible()
                        expect(document.get_by_text('Source: fictional-test-profile',exact=False)).to_be_visible()
                        expect(document).not_to_contain_text('Changed Python fact')
                        for fmt in ('pdf','docx','txt'):
                            link=document.get_by_role('link',name='Download '+fmt.upper(),exact=True)
                            self.assertIn('/profiles/fixture/documents/',link.get_attribute('href'))
                            with page.expect_download() as event:
                                link.click()
                            data=Path(event.value.path()).read_bytes()
                            self.assertEqual(hashlib.sha256(data).hexdigest(),row['sha256'][fmt])
                    self.assertFalse(external)
                    profile_before=store.profile('fixture')
                    page.get_by_role('button',name='Profile',exact=True).click()
                    library=page.get_by_role('region',name='Document library',exact=True)
                    expect(library.get_by_text('Generated application documents',exact=True)).to_be_visible()
                    expect(library.get_by_role('region',name='resume version 1',exact=True)).to_be_visible()
                    kinds=('BASE_RESUME','MASTER_RESUME','TAILORED_RESUME','COVER_LETTER',
                           'APPLICATION_ANSWER_EXPORT','SUPPORTING_DOCUMENT','CERTIFICATION')
                    for kind in kinds:
                        library.get_by_label('Document type',exact=True).select_option(kind)
                        library.get_by_label('Document label',exact=True).fill(kind+' example')
                        library.get_by_label('Choose a document',exact=False).set_input_files(
                            {'name':'synthetic.txt','mimeType':'text/plain','buffer':b'Original synthetic library file'})
                        library.get_by_role('button',name='Store document',exact=True).click()
                        group=library.get_by_role('region',name=kind+' example collection',exact=True)
                        expect(group.get_by_role('link',name='Download original version 1',exact=True)).to_be_visible()
                        expect(library.get_by_role('button',name='Refresh library',exact=True)).to_be_enabled()
                    library.get_by_label('Document collection',exact=True).select_option(label='CERTIFICATION example')
                    library.get_by_label('Choose a document',exact=False).set_input_files(
                        {'name':'revised.txt','mimeType':'text/plain','buffer':b'Revised synthetic library file'})
                    library.get_by_role('button',name='Store document',exact=True).click()
                    group=library.get_by_role('region',name='CERTIFICATION example collection',exact=True)
                    expect(group.get_by_role('link',name='Download original version 2',exact=True)).to_be_visible()
                    expect(library.get_by_role('button',name='Refresh library',exact=True)).to_be_enabled()
                    page.reload()
                    page.get_by_role('button',name='Profile',exact=True).click()
                    group=page.get_by_role('region',name='CERTIFICATION example collection',exact=True)
                    for version,data in [(1,b'Original synthetic library file'),(2,b'Revised synthetic library file')]:
                        with page.expect_download() as event:
                            group.get_by_role('link',name=f'Download original version {version}',exact=True).click()
                        self.assertEqual(Path(event.value.path()).read_bytes(),data)
                    self.assertEqual(store.profile('fixture'),profile_before)
                    self.assertEqual(len(context.request.get(origin+'/profiles/fixture/resumes').json()['resumes']),1)
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
