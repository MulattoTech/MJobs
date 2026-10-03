"""Real browser acceptance for the private development-feedback workstation."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from playwright.sync_api import sync_playwright, expect, Error


class FeedbackGUIBrowserTests(unittest.TestCase):
    def test_state_outage_restores_browser_draft_and_editing_ready_preserves_receipt(self):
        from cli.feedback_gui import create_server

        with tempfile.TemporaryDirectory() as directory:
            server = create_server(port=0, output_root=Path(directory) / 'feedback')
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            origin = f'http://127.0.0.1:{server.server_address[1]}'
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(origin)
                    page.locator('[data-task-id="02-quick-feedback"]').click()
                    page.get_by_label('Short title').fill('Original ready report')
                    expect(page.locator('#save-status')).to_contain_text('Saved on this computer')
                    page.route('**/api/state',lambda route:route.abort())
                    page.reload()
                    expect(page.get_by_label('Short title')).to_have_value('Original ready report')
                    expect(page.locator('#global-error')).to_contain_text('Restored browser drafts')
                    page.unroute('**/api/state')
                    for control in page.locator('#task-fields [required]').all():
                        control.fill('Synthetic required detail')
                    page.get_by_role('button',name='Ready for Codex',exact=True).click()
                    expect(page.locator('#handoff-result')).to_contain_text('Saved for Codex')
                    page.get_by_label('Short title').fill('Edited draft preserves original')
                    expect(page.locator('#save-status')).to_contain_text('Saved on this computer')
                    page.reload()
                    expect(page.get_by_role('button',name='Original ready report',exact=True)).to_be_visible()
                    expect(page.get_by_role('button',name='Edited draft preserves original',exact=True)).to_be_visible()
                    page.get_by_role('button',name='Original ready report',exact=True).click()
                    expect(page.locator('#handoff-result')).to_contain_text('Saved for Codex')
                    browser.close()
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_offline_recovery_and_conflicting_tabs_keep_both_versions(self):
        from cli.feedback_gui import create_server

        with tempfile.TemporaryDirectory() as directory:
            server = create_server(port=0, output_root=Path(directory) / 'feedback')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f'http://127.0.0.1:{server.server_address[1]}'
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto(origin)
                    page.locator('[data-task-id="02-quick-feedback"]').click()
                    page.get_by_label('Short title').fill('Offline draft')
                    expect(page.locator('#save-status')).to_contain_text('Saved on this computer')
                    def unavailable(route):
                        if route.request.method == 'POST': route.abort()
                        else: route.continue_()
                    context.route('**/api/reports', unavailable)
                    page.get_by_label('I was trying to').fill('My words while the server is unavailable')
                    expect(page.locator('#save-status')).to_contain_text('computer save needs retry')
                    page.reload()
                    expect(page.get_by_label('I was trying to')).to_have_value('My words while the server is unavailable')
                    context.unroute('**/api/reports', unavailable)
                    page.get_by_role('button', name='Save now', exact=True).click()
                    expect(page.locator('#save-status')).to_contain_text('Saved on this computer')
                    other = context.new_page()
                    other.goto(origin)
                    expect(other.locator('#global-error')).to_contain_text('already open in another tab')
                    expect(other.locator('[data-task-id]')).to_have_count(0)
                    other.close()
                    # A different browser/profile can edit, but optimistic revisions preserve both versions.
                    second_context = browser.new_context()
                    other = second_context.new_page()
                    other.goto(origin)
                    other.get_by_role('button',name='Offline draft',exact=True).click()
                    expect(other.get_by_label('Short title')).to_have_value('Offline draft')
                    page.get_by_label('Short title').fill('First window version')
                    expect(page.locator('#save-status')).to_contain_text('Saved on this computer')
                    other.get_by_label('Short title').fill('Second window version')
                    expect(other.locator('#conflict')).to_be_visible()
                    other.get_by_role('button', name='Keep my version as a new draft').click()
                    expect(other.locator('#save-status')).to_contain_text('Saved on this computer')
                    other.reload()
                    expect(other.get_by_role('button', name='First window version', exact=True)).to_be_visible()
                    expect(other.get_by_role('button', name='Second window version (recovered copy)', exact=True)).to_be_visible()
                    browser.close()
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_guided_report_survives_browser_and_server_restart(self):
        from cli.feedback_gui import create_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "feedback"
            server = create_server(port=0, output_root=output)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = f"http://127.0.0.1:{port}"
            errors = []
            try:
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(str(root / 'browser'), headless=True)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin)
                    expect(page.get_by_role('heading', name='Development & Feedback', exact=True)).to_be_visible()
                    expect(page.locator('[data-task-id]')).to_have_count(19)
                    page.locator('[data-task-id="03-bug-report"]').click()
                    page.get_by_label('Short title').fill('Synthetic regression: lost form')
                    # Immediate synchronous local recovery must not depend on a debounce completing.
                    page.locator('#task-fields textarea').first.fill('Typed immediately before closing the browser.')
                    # Crash the renderer, not just an orderly navigation/close.
                    with page.expect_event('crash', timeout=5000):
                        try:
                            page.goto('chrome://crash', timeout=5000)
                        except Error:
                            pass
                    context.close()

                    server.shutdown()
                    server.server_close()
                    thread.join()
                    server = create_server(port=port, output_root=output)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    context = pw.chromium.launch_persistent_context(str(root / 'browser'), headless=True)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin)
                    expect(page.get_by_label('Short title')).to_have_value('Synthetic regression: lost form')
                    expect(page.locator('#task-fields textarea').first).to_have_value('Typed immediately before closing the browser.')
                    expect(page.locator('#save-status')).to_contain_text('Saved on this computer')
                    page.get_by_role('button', name='Choose another task').click()
                    page.locator('[data-task-id="02-quick-feedback"]').click()
                    page.get_by_label('Short title').fill('Another independent draft')
                    expect(page.locator('#save-status')).to_contain_text('Saved on this computer')
                    page.get_by_role('button', name='Synthetic regression: lost form', exact=True).click()
                    expect(page.locator('#task-fields textarea').first).to_have_value('Typed immediately before closing the browser.')
                    # Complete the task-specific required fields from visible controls.
                    for control in page.locator('#task-fields [required]').all():
                        if control.evaluate('(e) => e.tagName') == 'SELECT':
                            options = control.locator('option').evaluate_all('(els) => els.map(e => e.value).filter(Boolean)')
                            control.select_option(options[0])
                        elif not control.input_value():
                            control.fill('Synthetic browser acceptance detail')
                    page.get_by_role('button', name='Ready for Codex', exact=True).click()
                    expect(page.locator('#handoff-result')).to_contain_text('Saved for Codex')
                    folders = list(output.glob('report-*'))
                    self.assertEqual(len(folders), 1)
                    self.assertIn('Synthetic regression: lost form', (folders[0] / 'feedback.md').read_text(encoding='utf-8'))
                    self.assertEqual(json.loads((folders[0] / 'session.json').read_text(encoding='utf-8'))['capture_status'], 'NOT_RUN')
                    page.reload()
                    expect(page.get_by_label('Short title')).to_have_value('Synthetic regression: lost form')
                    page.set_viewport_size({'width':390,'height':844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    expect(page.get_by_role('button', name='Ready for Codex', exact=True)).to_be_visible()
                    self.assertEqual(errors, [])
                    context.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
