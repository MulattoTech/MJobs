"""Actual UI opt-in traverses the durable API and document worker."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class SkillOrderBrowserTests(unittest.TestCase):
    def test_target_bullet_fallback_explicit_precedence_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'target-bullets.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            source = PROFILE['facts'][0]
            facts = [
                dict(source, id='python', kind='skill', text='Python'),
                dict(source, id='summary', kind='summary', text='Systems administrator.'),
                dict(source, id='responsibility-unmatched', kind='responsibility', text='Administered VMware hosts.'),
                dict(source, id='project-unmatched', kind='project', text='Built a Hyper-V lab.'),
                dict(source, id='unapproved-python', kind='responsibility', text='Built Python automation.',
                     verified=False, user_approved=False),
            ]
            store.save_profile(dict(PROFILE, facts=facts))
            profile_before = store.profile('fixture')
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            job = store.jobs()[0]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(skills=['Python']))
            application_id = prepare(store, job['id'], 'fixture', root)
            base_letter = next(row for row in application_documents(store, application_id)
                               if row['kind'] == 'cover-letter')
            base_letter_bytes = (root / application_id / base_letter['files']['txt']).read_bytes()

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
                        self.fail('Target-bullet test API did not start')
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
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
                    page.set_default_timeout(5000)
                    page.goto(origin)
                    page.get_by_label('Active profile', exact=True).select_option('fixture')
                    page.get_by_role('button').filter(has=page.get_by_role('heading', name=job['title'], exact=True)).click()
                    page.get_by_label('Order approved skills for this job', exact=True).check()
                    with page.expect_response(lambda response: response.url == origin + '/applications' and
                                              response.request.method == 'POST') as event:
                        page.get_by_role('button', name='Prepare documents', exact=True).click()
                    operation = event.value.json()
                    with patch.dict('os.environ', MJOBS_DATABASE_URL=store.url,
                                    MJOBS_MOCK_ORIGIN='http://127.0.0.1:1', MJOBS_OUTPUT_DIR=str(root),
                                    MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'], 'COMPLETED')
                    fallback_document = page.get_by_role('region', name='resume version 2', exact=True)
                    with page.expect_download() as fallback_download:
                        fallback_document.get_by_role('link', name='Download TXT', exact=True).click()
                    fallback_text = Path(fallback_download.value.path()).read_text(encoding='utf-8')
                    self.assertIn('Administered VMware hosts.', fallback_text)
                    self.assertIn('Built a Hyper-V lab.', fallback_text)
                    self.assertNotIn('Built Python automation.', fallback_text)
                    page.reload()
                    page.get_by_role('navigation').get_by_role('button', name='Applications', exact=False).first.click()
                    page.locator('.job-card').first.click()
                    fallback_document = page.get_by_role('region', name='resume version 2', exact=True)
                    expect(fallback_document).to_be_visible()
                    comparison = page.get_by_role('region', name='Compare base and tailored resumes', exact=True)
                    comparison.get_by_role('button', name='Compare selected resumes', exact=True).click()
                    expect(comparison.get_by_role('region', name='Content omitted', exact=True)).to_contain_text(
                        'No content omitted.')

                    page.get_by_role('navigation').get_by_role('button', name='Jobs', exact=False).click()
                    page.get_by_role('button').filter(has=page.get_by_role('heading', name=job['title'], exact=True)).click()
                    page.get_by_label('Order approved skills for this job', exact=True).check()
                    page.get_by_label('Choose resume facts', exact=True).check()
                    page.get_by_label('Include Python', exact=True).uncheck()
                    page.get_by_label('Include Systems administrator.', exact=True).uncheck()
                    page.get_by_label('Include Administered VMware hosts.', exact=True).uncheck()
                    page.get_by_label('Include Built a Hyper-V lab.', exact=True).check()
                    with page.expect_response(lambda response: response.url == origin + '/applications' and
                                              response.request.method == 'POST') as event:
                        page.get_by_role('button', name='Prepare documents', exact=True).click()
                    operation = event.value.json()
                    queued = event.value.request.post_data_json
                    self.assertEqual(queued['tailor_fact_ids'], ['project-unmatched'])
                    with patch.dict('os.environ', MJOBS_DATABASE_URL=store.url,
                                    MJOBS_MOCK_ORIGIN='http://127.0.0.1:1', MJOBS_OUTPUT_DIR=str(root),
                                    MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'], 'COMPLETED')
                    explicit_document = page.get_by_role('region', name='resume version 3', exact=True)
                    with page.expect_download() as explicit_download:
                        explicit_document.get_by_role('link', name='Download TXT', exact=True).click()
                    explicit_text = Path(explicit_download.value.path()).read_text(encoding='utf-8')
                    self.assertIn('Built a Hyper-V lab.', explicit_text)
                    self.assertNotIn('Administered VMware hosts.', explicit_text)
                    history = application_documents(store, application_id)
                    explicit_row = next(row for row in history if row['kind'] == 'resume' and row['version'] == 3)
                    page.reload()
                    page.get_by_role('navigation').get_by_role('button', name='Applications', exact=False).first.click()
                    page.locator('.job-card').first.click()
                    expect(page.get_by_role('region', name='resume version 3', exact=True)).to_be_visible()
                    comparison = page.get_by_role('region', name='Compare base and tailored resumes', exact=True)
                    comparison.get_by_label('Tailored version', exact=True).select_option(explicit_row['id'])
                    comparison.get_by_role('button', name='Compare selected resumes', exact=True).click()
                    omitted = comparison.get_by_role('region', name='Content omitted', exact=True)
                    expect(omitted).to_contain_text('Administered VMware hosts.')
                    expect(omitted).to_contain_text('Python')
                    expect(omitted).to_contain_text('Systems administrator.')
                    self.assertEqual(store.profile('fixture'), profile_before)
                    current_letter = next(row for row in application_documents(store, application_id)
                                          if row['kind'] == 'cover-letter' and row['version'] == 3)
                    self.assertEqual((root / application_id / current_letter['files']['txt']).read_bytes(),
                                     base_letter_bytes)
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_explicit_ordering_reaches_worker_and_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(f"sqlite:///{(root/'test.db').as_posix()}")
            store.migrate();store.bind_workspace_mode('FIXTURE')
            duty=dict(PROFILE['facts'][0],id='python-duty',kind='responsibility',text='Built Python APIs for internal teams.')
            store.save_profile(dict(PROFILE,facts=[PROFILE['facts'][1],PROFILE['facts'][0],duty],contact={'phones':['555 0100'],'city':'Tulsa'}))
            with fixture_server() as fixture:discover(store,fixture,'fixture')
            job=store.jobs()[0]
            with store.engine.begin() as c:c.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Python']))
            prepare(store,job['id'],'fixture',root)
            original=b'Alex Fixture\nalex@example.test\nSkills\nSQL; Python\nResponsibilities\nMaintained servers\n'
            uploaded=save_resume(store,root,'fixture','original.txt','text/plain',original)
            sock=bound_browser_socket();origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
            app=create_app(store,'http://127.0.0.1:1',root,'redis://127.0.0.1:1/0',operation_mode='FIXTURE')
            server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            try:
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline:self.fail('Skill-order test API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True);context=browser.new_context(accept_downloads=True)
                    external=[]
                    def local_only(route):
                        if urlsplit(route.request.url).netloc!=urlsplit(origin).netloc:
                            external.append(route.request.url);route.abort()
                        else:route.continue_()
                    context.route('**/*',local_only)
                    page=context.new_page();page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')");page.reload()
                    page.get_by_role('button').filter(has=page.get_by_role('heading',name=job['title'],exact=True)).click()
                    opt=page.get_by_label('Order approved skills for this job',exact=True)
                    expect(opt).not_to_be_checked();opt.check()
                    with page.expect_response(lambda response:response.url==origin+'/applications' and response.request.method=='POST') as event:
                        page.get_by_role('button',name='Prepare documents',exact=True).click()
                    operation=event.value.json()
                    with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',
                                    MJOBS_OUTPUT_DIR=str(root),MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
                    document=page.get_by_role('region',name='resume version 2',exact=True)
                    expect(document.get_by_text('Tailored resume: skill order',exact=False)).to_be_visible()
                    expect(document.get_by_text('Job-matched approved facts in bold: 2.',exact=False)).to_be_visible()
                    document.get_by_text('Generation details and facts',exact=True).click()
                    expect(document.get_by_text('Matched job skills: Python',exact=True)).to_have_count(2)
                    with page.expect_download() as downloaded:
                        document.get_by_role('link',name='Download TXT',exact=True).click()
                    self.assertEqual(Path(downloaded.value.path()).read_text(encoding='utf-8').splitlines(),
                        ['Alex Fixture','alex@example.test','Skills','Python','SQL','Experience','Built Python APIs for internal teams.'])
                    for _ in range(2):
                        comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                        compare_button=comparison.get_by_role('button',name='Compare selected resumes',exact=True)
                        expect(compare_button).to_be_enabled();compare_button.click()
                        result=comparison.get_by_role('region',name='Resume comparison result',exact=True)
                        expect(result.get_by_role('region',name='Reordering',exact=True)).to_contain_text('Python: fact position 2 → 1')
                        expect(result.get_by_role('region',name='Skills and bullets emphasized',exact=True)).to_contain_text('Python — matched Python')
                        expect(result.get_by_role('region',name='Skills and bullets emphasized',exact=True)).to_contain_text('Built Python APIs for internal teams. — matched Python')
                        expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('No content omitted.')
                        expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('No changes.')
                        expect(result.get_by_role('region',name='Bullet and fact changes',exact=True)).to_contain_text('No changes.')
                        page.reload();page.get_by_role('button',name='Applications',exact=False).first.click()
                        page.locator('.job-card').first.click()
                    comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                    comparison.get_by_label('Base resume',exact=True).select_option('u:'+uploaded['revision_id'])
                    comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                    result=comparison.get_by_role('region',name='Resume comparison result',exact=True)
                    expect(result.get_by_role('heading',name='Uploaded base · v1',exact=True)).to_be_visible()
                    expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('Maintained servers')
                    expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('Source section unavailable or ambiguous')
                    provenance=result.get_by_role('region',name='Original extraction provenance',exact=True)
                    expect(provenance).to_contain_text('Native extracted text')
                    with page.expect_download() as original_download:
                        provenance.get_by_role('link',name='Download compared original',exact=True).click()
                    self.assertEqual(Path(original_download.value.path()).read_bytes(),original)
                    application_id=store.applications('fixture')[0]['id']
                    profile_before_cancel=store.profile('fixture');history_before_cancel=application_documents(store,application_id)
                    comparison_started=threading.Event();comparison_cancelled=threading.Event()

                    def unconfirmed_cancel(route):
                        route.fulfill(status=200,content_type='application/json',body='{"cancelled":false}')

                    def finish_comparison(data,format,cancellation_event):
                        comparison_started.set();time.sleep(.5)
                        from services.career_profile.extraction import extract_resume_text
                        return extract_resume_text(data,format,cancellation_event)

                    page.route('**/uploaded-resume-comparison/cancel**',unconfirmed_cancel)
                    with patch('services.documents.uploaded_comparison.extract_resume_text',side_effect=finish_comparison):
                        comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                        self.assertTrue(comparison_started.wait(5),'uploaded comparison did not start extraction')
                        comparison.get_by_role('button',name='Cancel comparison',exact=True).click()
                        expect(comparison.get_by_role('status')).to_contain_text('Cancellation was not confirmed.')
                        expect(comparison.get_by_role('region',name='Resume comparison result',exact=True)).to_be_visible()
                    page.unroute('**/uploaded-resume-comparison/cancel**',unconfirmed_cancel)
                    self.assertEqual(store.profile('fixture'),profile_before_cancel)
                    self.assertEqual(application_documents(store,application_id),history_before_cancel)

                    def wait_for_comparison_cancel(data,format,cancellation_event):
                        comparison_started.set();deadline=time.monotonic()+5
                        while not cancellation_event.is_set() and time.monotonic()<deadline:time.sleep(.005)
                        if cancellation_event.is_set():
                            comparison_cancelled.set()
                            from services.career_profile.extraction import ResumeExtractionCancelled
                            raise ResumeExtractionCancelled()
                        raise AssertionError('comparison abort was not propagated into resume extraction')

                    with patch('services.documents.uploaded_comparison.extract_resume_text',side_effect=wait_for_comparison_cancel):
                        comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                        self.assertTrue(comparison_started.wait(5),'uploaded comparison did not start extraction')
                        comparison.get_by_role('button',name='Cancel comparison',exact=True).click()
                        expect(comparison.get_by_role('status')).to_contain_text('Comparison cancelled.')
                    self.assertTrue(comparison_cancelled.wait(5),'browser cancellation did not stop extraction work')
                    self.assertEqual(store.profile('fixture'),profile_before_cancel)
                    self.assertEqual(application_documents(store,application_id),history_before_cancel)
                    before=store.profile('fixture')
                    page.get_by_role('navigation').get_by_role('button',name='Jobs',exact=False).click()
                    page.get_by_role('button').filter(has=page.get_by_role('heading',name=job['title'],exact=True)).click()
                    page.get_by_label('Order approved skills for this job',exact=True).check()
                    page.get_by_label('Choose resume facts',exact=True).check()
                    expect(page.get_by_label('Include SQL',exact=True)).to_be_checked()
                    page.get_by_label('Include SQL',exact=True).uncheck()
                    page.get_by_label('Include Python',exact=True).uncheck()
                    page.get_by_label('Include Built Python APIs for internal teams.',exact=True).uncheck()
                    operation_count=store.count(m.operations.name)
                    page.get_by_role('button',name='Prepare documents',exact=True).click()
                    expect(page.get_by_text('Reload approved facts and select at least one fact for this resume.',exact=True)).to_be_visible()
                    self.assertEqual(store.count(m.operations.name),operation_count)
                    page.get_by_label('Include Python',exact=True).check()
                    with page.expect_response(lambda response:response.url==origin+'/applications' and response.request.method=='POST') as event:
                        page.get_by_role('button',name='Prepare documents',exact=True).click()
                    operation=event.value.json()
                    with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(root),MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
                    selected=page.get_by_role('region',name='resume version 3',exact=True)
                    expect(selected.get_by_text('Tailored resume: selected facts',exact=False)).to_be_visible()
                    expect(selected.get_by_text('Approved facts included: 1. Omitted from this resume: 2.',exact=False)).to_be_visible()
                    with page.expect_download() as subset_download:
                        selected.get_by_role('link',name='Download TXT',exact=True).click()
                    self.assertEqual(Path(subset_download.value.path()).read_text(encoding='utf-8').splitlines(),['Alex Fixture','alex@example.test','Skills','Python'])
                    comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                    comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                    expect(comparison.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('skill: SQL')
                    self.assertEqual(store.profile('fixture'),before)
                    page.get_by_role('navigation').get_by_role('button',name='Jobs',exact=False).click()
                    page.get_by_role('button').filter(has=page.get_by_role('heading',name=job['title'],exact=True)).click()
                    contact_option=page.get_by_label('Include saved resume contact details',exact=True)
                    expect(contact_option).not_to_be_checked();contact_option.check()
                    with page.expect_response(lambda response:response.url==origin+'/applications' and response.request.method=='POST') as event:
                        page.get_by_role('button',name='Prepare documents',exact=True).click()
                    operation=event.value.json()
                    with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(root),MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
                    contact_document=page.get_by_role('region',name='resume version 4',exact=True)
                    contact_document.get_by_text('Generation details and facts',exact=True).click()
                    expect(contact_document.get_by_role('region',name='Saved resume contact header')).to_contain_text('Phone: 555 0100')
                    with page.expect_download() as contact_download:
                        contact_document.get_by_role('link',name='Download TXT',exact=True).click()
                    self.assertIn('Phone: 555 0100',Path(contact_download.value.path()).read_text(encoding='utf-8'))
                    self.assertEqual(store.profile('fixture'),before)
                    page.get_by_role('navigation').get_by_role('button',name='Jobs',exact=False).click()
                    page.get_by_role('button').filter(has=page.get_by_role('heading',name=job['title'],exact=True)).click()
                    page.get_by_label('Order approved skills for this job',exact=True).set_checked(True)
                    page.get_by_label('Choose resume facts',exact=True).set_checked(True)
                    duty='Built Python APIs for internal teams.'
                    page.get_by_label('Include '+duty,exact=True).set_checked(True)
                    rewrite='Delivered Python API services for internal teams.'
                    page.get_by_label('Rewrite this bullet: '+duty,exact=True).fill(rewrite)
                    page.get_by_label('I reviewed this rewrite: '+duty,exact=True).check()
                    with page.expect_response(lambda response:response.url==origin+'/applications' and response.request.method=='POST') as event:
                        page.get_by_role('button',name='Prepare documents',exact=True).click()
                    operation=event.value.json()
                    queued=event.value.request.post_data_json
                    self.assertEqual(queued['tailor_fact_rewrites'],[{'fact_id':'python-duty','text':rewrite,'reviewed':True}])
                    with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',
                                    MJOBS_OUTPUT_DIR=str(root),MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
                    rewritten=page.get_by_role('region',name='resume version 5',exact=True)
                    with page.expect_download() as rewritten_download:
                        rewritten.get_by_role('link',name='Download TXT',exact=True).click()
                    self.assertIn(rewrite,Path(rewritten_download.value.path()).read_text(encoding='utf-8'))
                    page.reload();page.get_by_role('navigation').get_by_role('button',name='Applications',exact=False).first.click()
                    page.locator('.job-card').first.click()
                    comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                    comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                    changes=comparison.get_by_role('region',name='Bullet and fact changes',exact=True)
                    expect(changes).to_contain_text(duty);expect(changes).to_contain_text(rewrite)
                    self.assertEqual(store.profile('fixture'),before)
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close();store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_user_reviewed_technology_experience_rewrite_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(f"sqlite:///{(root/'summary.db').as_posix()}")
            from alembic import command
            from alembic.config import Config
            with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url):
                command.upgrade(Config('alembic.ini'),'head')
            store.bind_workspace_mode('FIXTURE')
            summary=dict(PROFILE['facts'][0],id='linux-summary',kind='summary',
                text='Systems engineer with 12 years of Linux experience, 2012-2024.')
            project=dict(PROFILE['facts'][0],id='recovery-project',kind='project',
                text='Built a local-first recovery harness for 3 environments in 2021.')
            achievement=dict(PROFILE['facts'][0],id='recovery-achievement',kind='accomplishment',
                text='Reduced recovery time by 25% in 2022.')
            technology=dict(PROFILE['facts'][0],id='postgres-experience',kind='technology_experience',
                text='Administered PostgreSQL databases for 6 teams from 2020-2024.')
            skill=dict(PROFILE['facts'][0],id='linux',kind='skill',text='Linux')
            store.save_profile(dict(PROFILE,facts=[summary,project,achievement,technology,skill]))
            with fixture_server() as fixture:discover(store,fixture,'fixture')
            job=store.jobs()[0]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Linux']))
            prepare(store,job['id'],'fixture',root)
            before=store.profile('fixture')
            sock=bound_browser_socket();origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
            app=create_app(store,'http://127.0.0.1:1',root,'redis://127.0.0.1:1/0',operation_mode='FIXTURE')
            server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            try:
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline:self.fail('Summary-adjustment test API did not start')
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True);context=browser.new_context(accept_downloads=True)
                    external=[]
                    def local_only(route):
                        if urlsplit(route.request.url).netloc!=urlsplit(origin).netloc:
                            external.append(route.request.url);route.abort()
                        else:route.continue_()
                    context.route('**/*',local_only)
                    page=context.new_page();page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')");page.reload()
                    page.get_by_role('button').filter(has=page.get_by_role('heading',name=job['title'],exact=True)).click()
                    page.get_by_label('Order approved skills for this job',exact=True).check()
                    page.get_by_label('Choose resume facts',exact=True).check()
                    rewrites=[
                        (summary,'Linux systems engineer with 12 years of experience, 2012-2024.','summary'),
                        (project,'Created a recovery harness for 3 environments in 2021.','project bullet'),
                        (achievement,'Cut recovery time by 25% in 2022.','accomplishment bullet'),
                        (technology,'Managed PostgreSQL databases for 6 teams from 2020-2024.','technology experience'),
                    ]
                    for fact,wording,label in rewrites:
                        page.get_by_label('Include '+fact['text'],exact=True).check()
                        page.get_by_label(f'Rewrite this {label}: '+fact['text'],exact=True).fill(wording)
                        page.get_by_label('I reviewed this rewrite: '+fact['text'],exact=True).check()
                    with page.expect_response(lambda response:response.url==origin+'/applications' and response.request.method=='POST') as event:
                        page.get_by_role('button',name='Prepare documents',exact=True).click()
                    operation=event.value.json();queued=event.value.request.post_data_json
                    self.assertEqual(queued['tailor_fact_rewrites'],[
                        {'fact_id':fact['id'],'text':wording,'reviewed':True} for fact,wording,_ in rewrites])
                    with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',
                                    MJOBS_OUTPUT_DIR=str(root),MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
                    tailored_document=page.get_by_role('region',name='resume version 2',exact=True)
                    with page.expect_download() as download:
                        tailored_document.get_by_role('link',name='Download TXT',exact=True).click()
                    rendered=Path(download.value.path()).read_text(encoding='utf-8')
                    for fact,wording,_ in rewrites:
                        self.assertIn(wording,rendered)
                        self.assertNotIn(fact['text'],rendered)
                    page.reload();page.get_by_role('navigation').get_by_role('button',name='Applications',exact=False).first.click()
                    page.locator('.job-card').first.click()
                    comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                    comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                    summary_changes=comparison.get_by_role('region',name='Summary changes',exact=True)
                    expect(summary_changes).to_contain_text(summary['text'])
                    expect(summary_changes).to_contain_text(rewrites[0][1])
                    bullet_changes=comparison.get_by_role('region',name='Bullet and fact changes',exact=True)
                    for fact,wording,_ in rewrites[1:]:
                        expect(bullet_changes).to_contain_text(fact['text'])
                        expect(bullet_changes).to_contain_text(wording)
                    self.assertEqual(store.profile('fixture'),before)
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close();store.engine.dispose()

    def test_responsibility_matched_summary_survives_prepare_reload_and_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(f"sqlite:///{(root/'responsibility-summary.db').as_posix()}")
            store.migrate();store.bind_workspace_mode('FIXTURE')
            duty='Manage backup systems across regional offices and document recovery plans'
            facts=[
                dict(PROFILE['facts'][0],id='summary-duty',kind='summary',text=f'Infrastructure administrator. {duty}.'),
                dict(PROFILE['facts'][0],id='summary-sales',kind='summary',text='Experienced retail sales associate.'),
                PROFILE['facts'][0],PROFILE['facts'][1],
            ]
            store.save_profile(dict(PROFILE,facts=facts));before=store.profile('fixture')
            with fixture_server() as fixture:discover(store,fixture,'fixture')
            job=store.jobs()[0]
            description=f'Responsibilities:\n- {duty}\nRequirements:\n- Five years of experience.'
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(
                    skills=['Python'],description=description))
            application_id=prepare(store,job['id'],'fixture',root)
            base_letter=next(row for row in application_documents(store,application_id)
                             if row['kind']=='cover-letter')
            base_letter_bytes=(root/application_id/base_letter['files']['txt']).read_bytes()
            sock=bound_browser_socket();origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
            app=create_app(store,'http://127.0.0.1:1',root,'redis://127.0.0.1:1/0',operation_mode='FIXTURE')
            server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            try:
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline:self.fail('Responsibility-summary API did not start')
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True);context=browser.new_context(accept_downloads=True)
                    external=[]
                    def local_only(route):
                        if urlsplit(route.request.url).netloc!=urlsplit(origin).netloc:
                            external.append(route.request.url);route.abort()
                        else:route.continue_()
                    context.route('**/*',local_only)
                    page=context.new_page();page.set_default_timeout(5000);page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')");page.reload()
                    page.get_by_role('button').filter(has=page.get_by_role('heading',name=job['title'],exact=True)).click()
                    page.get_by_label('Order approved skills for this job',exact=True).check()
                    with page.expect_response(lambda response:response.url==origin+'/applications' and
                                              response.request.method=='POST') as event:
                        page.get_by_role('button',name='Prepare documents',exact=True).click()
                    operation=event.value.json()
                    with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',
                                    MJOBS_OUTPUT_DIR=str(root),MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
                    resume=page.get_by_role('region',name='resume version 2',exact=True)
                    with page.expect_download() as download:resume.get_by_role('link',name='Download TXT',exact=True).click()
                    rendered=Path(download.value.path()).read_text(encoding='utf-8')
                    self.assertIn(duty,rendered);self.assertNotIn('Experienced retail sales associate.',rendered)
                    page.reload()
                    page.get_by_role('navigation').get_by_role('button',name='Applications',exact=False).first.click()
                    page.locator('.job-card').first.click()
                    comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                    comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                    omitted=comparison.get_by_role('region',name='Content omitted',exact=True)
                    expect(omitted).to_contain_text('Experienced retail sales associate.')
                    self.assertEqual(store.profile('fixture'),before)
                    letter=next(row for row in application_documents(store,application_id)
                                if row['kind']=='cover-letter')
                    self.assertEqual((root/application_id/letter['files']['txt']).read_bytes(),base_letter_bytes)
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close();store.engine.dispose()
                self.assertFalse(thread.is_alive())
