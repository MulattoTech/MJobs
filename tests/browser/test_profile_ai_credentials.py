"""Built UI -> encrypted key -> explicit scoring plan -> real durable adapter path."""
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from tests.integration import test_profile_ai_credentials as contract
from workers.tasks import execute_operation


class ProfileAICredentialBrowserTests(unittest.TestCase):
    def test_explicit_saved_key_selection_rotation_worker_and_history_reload(self):
        fixture = contract.ProfileAICredentialTests()
        fixture.setUp()
        fixture.response_body['job_evidence'] = [{'field': 'description', 'quote': 'Python SQL synthetic private sentinel.'}]
        self.addCleanup(fixture.doCleanups)
        fixture.store.save_ai_task_settings('fixture', {'job_scoring': {'provider': 'disabled', 'model': None}})
        fixture.client.delete('/profiles/fixture/provider-credentials/openai')
        sock = bound_browser_socket()
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(create_app(fixture.store, 'http://127.0.0.1:1',
            fixture.case.root, 'redis://127.0.0.1:1/0', operation_mode='PERSONAL'), log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    self.fail('Synthetic credential API did not start')
                time.sleep(.02)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    external, errors = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    page.route('**/*', local_only)
                    page.goto(origin, wait_until='domcontentloaded')
                    page.locator('#active-profile').select_option('fixture')
                    page.get_by_role('button', name='AI Settings', exact=True).click()
                    credentials = page.get_by_role('region', name='Provider credentials')
                    openai = credentials.get_by_role('group', name='OpenAI')
                    field = openai.get_by_label('OpenAI credential', exact=True)
                    field.fill(fixture.secret)
                    openai.get_by_role('button', name='Save credential', exact=True).click()
                    expect(field).to_have_value('')
                    panel = page.get_by_role('region', name='AI cost controls', exact=True)
                    panel.get_by_role('button', name='Preview AI work plan', exact=True).click()
                    preview = panel.get_by_role('region', name='Planned AI candidates', exact=True)
                    expect(preview).to_contain_text('AI provider disabled')
                    self.assertEqual(fixture.requests, [])
                    page.get_by_label('Job scoring provider', exact=True).select_option('openai')
                    page.get_by_label('Job scoring model', exact=True).fill('synthetic')
                    page.get_by_role('button', name='Save AI task settings', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='AI task settings saved')).to_be_visible()
                    expect(preview).to_have_count(0)
                    panel.get_by_role('button', name='Preview AI work plan', exact=True).click()
                    expect(preview).to_contain_text("this profile's saved OpenAI credential")
                    expect(preview).to_contain_text('https://api.openai.com')
                    field.fill('synthetic-browser-rotated-key')
                    openai.get_by_role('button', name='Rotate credential', exact=True).click()
                    expect(preview).to_have_count(0)
                    panel.get_by_role('button', name='Preview AI work plan', exact=True).click()
                    panel.get_by_role('button', name='Evaluate planned AI candidates', exact=True).click()
                    expect(panel.get_by_role('region', name='Durable AI evaluations')).to_contain_text('QUEUED')
                    with fixture.store.engine.connect() as connection:
                        operation = connection.execute(select(m.operations.c.id).where(
                            m.operations.c.kind == 'AI_EVALUATE_QUEUED')).scalar_one()
                    self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
                    self.assertEqual(len(fixture.requests), 1)
                    self.assertEqual(fixture.requests[0].headers['authorization'], 'Bearer synthetic-browser-rotated-key')
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='AI Settings', exact=True).click()
                    history = page.get_by_role('region', name='AI evaluation history', exact=True)
                    expect(history).to_contain_text("this profile's saved OpenAI credential")
                    expect(history).to_contain_text('Synthetic controlled transport response')
                    quotes = history.get_by_role('region', name='Checked posting quotes')
                    expect(quotes).to_contain_text('Python SQL synthetic private sentinel.')
                    expect(quotes).to_contain_text('Only the quotations are verified')
                    expect(quotes).to_contain_text('Source: captured job description')
                    expect(field).to_have_value('')
                    self.assertNotIn('synthetic-browser-rotated-key', page.locator('body').inner_text())
                    panel.get_by_role('button', name='Preview AI work plan', exact=True).click()
                    panel.get_by_role('button', name='Evaluate planned AI candidates', exact=True).click()
                    expect(panel.get_by_role('region', name='Durable AI evaluations')).to_contain_text('QUEUED')
                    with fixture.store.engine.connect() as connection:
                        stale_operation = connection.execute(select(m.operations.c.id).where(
                            m.operations.c.kind == 'AI_EVALUATE_QUEUED',
                            m.operations.c.id != operation)).scalar_one()
                    field.fill('synthetic-revoked-before-worker-key')
                    openai.get_by_role('button', name='Rotate credential', exact=True).click()
                    expect(field).to_have_value('')
                    self.assertEqual(execute_operation(stale_operation)['status'], 'BLOCKED')
                    expect(panel.get_by_role('region', name='Durable AI evaluations')).to_contain_text('The saved credential changed, so this batch stopped')
                    self.assertEqual(len(fixture.requests), 1)
                    page.locator('#active-profile').select_option('other')
                    expect(history).to_contain_text('No saved AI evaluations for this profile')
                    expect(credentials.get_by_role('group', name='OpenAI')).to_contain_text('No credential saved')
                    self.assertEqual(external, [])
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive(), 'Synthetic credential API must stop')
