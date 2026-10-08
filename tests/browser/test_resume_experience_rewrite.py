"""Connected synthetic browser proof for an individually reviewed experience rewrite."""
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
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class ResumeExperienceRewriteBrowserTests(unittest.TestCase):
    def test_reviewed_experience_rewrite_reaches_durable_resume_history_and_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(f"sqlite:///{(root/'experience.db').as_posix()}")
            store.migrate();store.bind_workspace_mode('FIXTURE')
            experience=dict(PROFILE['facts'][0],id='experience',kind='experience',
                text='Supported 12 clinics with Linux systems from 2020-2024.')
            skill=dict(PROFILE['facts'][0],id='linux',kind='skill',text='Linux')
            store.save_profile(dict(PROFILE,facts=[experience,skill]))
            with fixture_server() as fixture:discover(store,fixture,'fixture')
            job=store.jobs()[0]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Linux']))
            app_id=prepare(store,job['id'],'fixture',root)
            before=store.profile('fixture')
            sock=bound_browser_socket();origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
            api=create_app(store,'http://127.0.0.1:1',root,'redis://127.0.0.1:1/0',operation_mode='FIXTURE')
            server=uvicorn.Server(uvicorn.Config(api,log_level='error'))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            try:
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline:
                        self.fail('Experience-rewrite API did not start')
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True)
                    context=browser.new_context(accept_downloads=True)
                    external=[]
                    def local_only(route):
                        if urlsplit(route.request.url).netloc!=urlsplit(origin).netloc:
                            external.append(route.request.url);route.abort()
                        else:route.continue_()
                    context.route('**/*',local_only)
                    page=context.new_page();page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')");page.reload()
                    expect(page.locator('.job-grid .job-list .job-card').first).to_be_visible()
                    page.locator('.job-grid .job-list .job-card').first.click()
                    page.get_by_label('Order approved skills for this job',exact=True).check()
                    page.get_by_label('Choose resume facts',exact=True).check()
                    page.get_by_label('Include '+skill['text'],exact=True).uncheck()
                    page.get_by_label('Include '+experience['text'],exact=True).check()
                    wording='Maintained Linux systems for 12 clinics from 2020-2024.'
                    page.get_by_label('Rewrite this experience: '+experience['text'],exact=True).fill(wording)
                    page.get_by_label('I reviewed this rewrite: '+experience['text'],exact=True).check()
                    with page.expect_response(lambda response:response.url==origin+'/applications' and
                                              response.request.method=='POST') as event:
                        page.get_by_role('button',name='Prepare documents',exact=True).click()
                    operation=event.value.json();payload=event.value.request.post_data_json
                    self.assertEqual(payload['tailor_fact_ids'],['experience'])
                    self.assertEqual(payload['tailor_fact_rewrites'],[
                        {'fact_id':'experience','text':wording,'reviewed':True}])
                    with patch.dict('os.environ',MJOBS_DATABASE_URL=store.url,
                        MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(root),
                        MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
                    rows=application_documents(store,app_id)
                    tailored=next(row for row in rows if row['kind']=='resume' and row['version']==2)
                    self.assertEqual(tailored['generation_context']['fact_rephrases'][0]['fact_kind'],'experience')
                    document=page.get_by_role('region',name='resume version 2',exact=True)
                    with page.expect_download() as download:
                        document.get_by_role('link',name='Download TXT',exact=True).click()
                    rendered=Path(download.value.path()).read_text(encoding='utf-8')
                    self.assertIn(wording,rendered);self.assertNotIn(experience['text'],rendered)
                    page.reload()
                    page.get_by_role('navigation').get_by_role(
                        'button',name='Applications',exact=False).first.click()
                    page.locator('.job-card').first.click()
                    comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                    comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                    changes=comparison.get_by_role('region',name='Bullet and fact changes',exact=True)
                    expect(changes).to_contain_text(experience['text'])
                    expect(changes).to_contain_text(wording)
                    self.assertEqual(store.profile('fixture'),before)
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close();store.engine.dispose()
                self.assertFalse(thread.is_alive())
