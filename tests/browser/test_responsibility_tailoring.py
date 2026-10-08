"""Synthetic browser proof for visible responsibility-selection provenance."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ResponsibilityTailoringBrowserTests(unittest.TestCase):
    def test_history_displays_the_exact_selected_job_duty(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(f"sqlite:///{(root/'responsibility.db').as_posix()}")
            store.migrate();store.bind_workspace_mode('FIXTURE')
            source=PROFILE['facts'][0]
            store.save_profile(dict(PROFILE,facts=[
                dict(source,id='nightly-duty',kind='responsibility',
                     text='Maintain nightly database backups for four clinics.'),
                dict(source,id='unrelated-duty',kind='experience',text='Coordinated a server refresh.'),
            ]))
            with fixture_server() as fixture:
                discover(store,fixture,'fixture')
            job=store.jobs()[0]
            description=('Systems Administrator\nResponsibilities:\n'
                         '- Maintain nightly database backups for four clinics.\n'
                         'Qualifications:\n- Experience with Kubernetes.')
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(
                    skills=['Kubernetes'],description=description))
            application_id=prepare(store,job['id'],'fixture',root)
            prepare(store,job['id'],'fixture',root,tailor_resume=True)
            app=create_app(store,'http://127.0.0.1:1',root,'redis://127.0.0.1:1/0',operation_mode='FIXTURE')
            sock=bound_browser_socket();origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
            server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            try:
                deadline=time.monotonic()+10
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>deadline:
                        self.fail('Responsibility history API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True)
                    context=browser.new_context()
                    context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                    page=context.new_page();page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')");page.reload()
                    page.get_by_role('button',name='Applications',exact=False).first.click()
                    page.get_by_role('region',name='Not tracked',exact=True).get_by_role(
                        'button',name=job['title'],exact=False).click()
                    document=page.get_by_role('region',name='resume version 2',exact=True)
                    document.get_by_text('Generation details and facts',exact=True).click()
                    expect(document.get_by_text(
                        'Selected for this job responsibility: maintain nightly database backups for four clinics',
                        exact=True)).to_be_visible()
                    context.close();browser.close()
            finally:
                server.should_exit=True;thread.join(timeout=10);sock.close();store.engine.dispose()
                self.assertFalse(thread.is_alive())
