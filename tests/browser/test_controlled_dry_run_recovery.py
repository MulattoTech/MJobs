"""Mounted cancellation and acknowledged retry retain identities across lost replies."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from tests.integration import test_controlled_dry_run_recovery as contract


class ControlledDryRunRecoveryBrowserTests(unittest.TestCase):
    _new_store=contract.ControlledDryRunRecoveryTests._new_store
    setUp=contract.ControlledDryRunRecoveryTests.setUp
    tearDown=contract.ControlledDryRunRecoveryTests.tearDown
    execute=contract.ControlledDryRunRecoveryTests.execute

    def test_cancel_reload_lost_retry_response_and_actual_worker(self):
        sock=bound_browser_socket(); origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(self.client.app,log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if not thread.is_alive() or time.monotonic()>deadline:self.fail('Owned fixture API did not start')
                time.sleep(.02)
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                try:
                    page=browser.new_page(); external=[]; errors=[]; retries=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def local_only(route):
                        if urlsplit(route.request.url).netloc!=urlsplit(origin).netloc:
                            external.append(route.request.url); route.abort()
                        else:route.continue_()
                    page.route('**/*',local_only)
                    page.add_init_script("localStorage.setItem('mjobs-profile','fixture')")
                    app=self.store.application(self.application_id)
                    job=self.store.job(app['job_id'],profile_id='fixture')
                    def open_panel():
                        page.goto(origin,wait_until='domcontentloaded')
                        page.get_by_role('button',name='Applications',exact=False).click()
                        page.get_by_role('region',name='Application pipeline').get_by_role('button',name=job['title'],exact=False).click()
                        return page.get_by_role('region',name='Controlled ATS preparation',exact=True)
                    panel=open_panel()
                    panel.get_by_role('checkbox',name='I reviewed these fixture values and selected documents',exact=True).check()
                    panel.get_by_role('button',name='Record fixture review',exact=True).click()
                    expect(panel.get_by_text('Fixture review saved.',exact=True)).to_be_visible()
                    panel.get_by_role('button',name='Run controlled fixture',exact=True).click()
                    expect(panel.get_by_text('Fixture run: QUEUED',exact=True)).to_be_visible()
                    with self.store.engine.begin() as connection:
                        original_url=connection.execute(select(m.routes.c.url).where(m.routes.c.id==app['route_id'])).scalar_one()
                        connection.execute(update(m.routes).where(m.routes.c.id==app['route_id']).values(url='https://unsupported.example.test/changed-route'))
                    panel=open_panel()
                    expect(panel).to_contain_text('You can still cancel a saved active run')
                    panel.get_by_role('button',name='Cancel fixture run',exact=True).click()
                    expect(panel.get_by_text('Fixture run: CANCELLED',exact=True)).to_be_visible()
                    with self.store.engine.begin() as connection:
                        connection.execute(update(m.routes).where(m.routes.c.id==app['route_id']).values(url=original_url))
                    panel=open_panel()
                    expect(panel.get_by_text('Fixture run: CANCELLED',exact=True)).to_be_visible()
                    expect(panel.get_by_role('button',name='Run controlled fixture',exact=True)).to_be_disabled()
                    expect(panel.get_by_role('button',name='Run reviewed retry',exact=True)).to_be_disabled()
                    panel.get_by_role('checkbox',name='I reviewed current inputs and want a new fixture attempt',exact=True).check()
                    def lose_reply(route):
                        retries.append(route.request.post_data)
                        response=route.fetch(); self.assertEqual(response.status,202)
                        route.abort('failed')
                    page.route('**/controlled-dry-run/runs/*/retry',lose_reply,times=1)
                    panel.get_by_role('button',name='Run reviewed retry',exact=True).click()
                    expect(panel.get_by_text('The request could not be confirmed. Retry to recover the same request, or refresh its saved result.',exact=True)).to_be_visible()
                    with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/retry')) as admission:
                        panel.get_by_role('button',name='Run reviewed retry',exact=True).click()
                    self.assertEqual(admission.value.request.post_data,retries[0])
                    queued=admission.value.json()
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        result=pool.submit(self.execute,queued['operation_id']).result(timeout=60)
                    self.assertEqual(result['status'],'COMPLETED',result)
                    panel=open_panel()
                    expect(panel.get_by_role('region',name='Saved fixture verification')).to_be_visible()
                    expect(panel).to_contain_text('earlier attempt remains in durable history')
                    with self.store.engine.connect() as connection:
                        states=connection.execute(select(m.operations.c.state).where(m.operations.c.kind=='APPLICATION_DRY_RUN',m.operations.c.entity_id==self.application_id)).scalars().all()
                    self.assertCountEqual(states,['CANCELLED','COMPLETED'])
                    self.assertEqual(self.store.count('application_attempts'),0)
                    self.assertEqual(external,[]);self.assertEqual(errors,[])
                finally:browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=10);sock.close()
            self.assertFalse(thread.is_alive(),'Owned fixture API did not stop')
