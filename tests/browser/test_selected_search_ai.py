"""Real mounted search selection, frozen advisory work, reload and stale preview."""
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from tests.integration import test_selected_search_ai as fixture


class SelectedSearchAIBrowserTests(fixture.SelectedSearchAIHarness):
    def test_selection_stale_preview_worker_and_history_reload(self):
        sock = bound_browser_socket()
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(create_app(self.store, 'http://127.0.0.1:1',
            self.root, 'redis://127.0.0.1:1/0', operation_mode='PERSONAL'), log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    self.fail('Synthetic API did not start')
                time.sleep(.02)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    external = []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url); route.abort()
                        else:
                            route.continue_()
                    page.route('**/*', local_only)
                    page.goto(origin, wait_until='domcontentloaded')
                    page.locator('#active-profile').select_option('fixture')
                    page.get_by_role('button',name='AI Settings',exact=True).click()
                    panel = page.get_by_role('region',name='AI cost controls',exact=True)
                    selector = panel.get_by_label('AI candidate search',exact=True)
                    selector.select_option('systems')
                    evaluate = panel.get_by_role('button',name='Evaluate planned AI candidates',exact=True)
                    expect(evaluate).to_be_disabled()
                    panel.get_by_role('button',name='Preview AI work plan',exact=True).click()
                    preview = panel.get_by_role('region',name='Planned AI candidates',exact=True)
                    expect(preview).to_contain_text('Infrastructure engineer')
                    selector.select_option('cloud')
                    expect(evaluate).to_be_disabled()
                    expect(preview).to_have_count(0)
                    panel.get_by_role('button',name='Preview AI work plan',exact=True).click()
                    expect(preview).to_contain_text('Cloud engineer')
                    self.assertEqual(self.calls,[])
                    with self.store.engine.begin() as c:
                        c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(name='Cloud revised'))
                    evaluate.click()
                    expect(panel).to_contain_text('the previous plan is stale')
                    expect(evaluate).to_be_disabled()
                    panel.get_by_role('button',name='Preview AI work plan',exact=True).click()
                    expect(preview).to_contain_text('Cloud revised')
                    evaluate.click()
                    expect(panel.get_by_role('region',name='Durable AI evaluations')).to_contain_text('QUEUED')
                    with self.store.engine.connect() as c:
                        operation=c.execute(select(m.operations.c.id).where(m.operations.c.kind=='AI_EVALUATE_QUEUED')).scalar_one()
                    with self.store.engine.begin() as c:
                        c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(name='Changed after admission'))
                    self.assertEqual(self.run_worker(operation)['status'],'COMPLETED')
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button',name='AI Settings',exact=True).click()
                    history=page.get_by_role('region',name='AI evaluation history',exact=True)
                    expect(history).to_contain_text('Cloud revised')
                    expect(history).to_contain_text('Cloud engineer')
                    expect(history).not_to_contain_text('Changed after admission')
                    history.get_by_text('Why this candidate was selected',exact=True).click()
                    expect(history).to_contain_text('ai_intensity: 95')
                    self.run_worker(operation)
                    self.assertEqual(len(self.calls),1)
                    self.assertEqual(external,[])
                finally:
                    browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=10);sock.close()
            self.assertFalse(thread.is_alive(),'Synthetic API must stop')
