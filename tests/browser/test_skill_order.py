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
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class SkillOrderBrowserTests(unittest.TestCase):
    def test_explicit_ordering_reaches_worker_and_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(f"sqlite:///{(root/'test.db').as_posix()}")
            store.migrate();store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE,facts=[PROFILE['facts'][1],PROFILE['facts'][0]]))
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
                    document.get_by_text('Generation details and facts',exact=True).click()
                    expect(document.get_by_text('Matched job skills: Python',exact=True)).to_be_visible()
                    with page.expect_download() as downloaded:
                        document.get_by_role('link',name='Download TXT',exact=True).click()
                    self.assertEqual(Path(downloaded.value.path()).read_text(encoding='utf-8').splitlines(),
                        ['Alex Fixture','alex@example.test','Python','SQL'])
                    for _ in range(2):
                        comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
                        comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
                        result=comparison.get_by_role('region',name='Resume comparison result',exact=True)
                        expect(result.get_by_role('region',name='Reordering',exact=True)).to_contain_text('Python: fact position 2 → 1')
                        expect(result.get_by_role('region',name='Skills emphasized',exact=True)).to_contain_text('Python — matched Python')
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
                    self.assertFalse(external)
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close();store.engine.dispose()
                self.assertFalse(thread.is_alive())
