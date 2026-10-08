"""Browser acceptance for revision archive history, restore, and API failures."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class DocumentArchiveRecoveryBrowserTests(unittest.TestCase):
    def test_specific_uploaded_revision_archives_restores_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory(prefix='mjobs-document-archive-recovery-') as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('PERSONAL')
            store.save_profile(dict(PROFILE))

            prior_revision_bytes = b'Synthetic earlier cover letter revision\n'
            original_bytes = b'Synthetic archive recovery revision bytes\n'
            control_bytes = b'Synthetic cover letter that remains active\n'
            prior_revision = save_resume(
                store, root, PROFILE['id'], 'reviewed-cover-letter.txt', 'text/plain',
                prior_revision_bytes, document_kind='COVER_LETTER',
                label='Archive recovery target',
            )
            archived_revision = save_resume(
                store, root, PROFILE['id'], 'reviewed-cover-letter.txt', 'text/plain',
                original_bytes, series_id=prior_revision['series_id'],
                document_kind='COVER_LETTER',
            )
            active_control = save_resume(
                store, root, PROFILE['id'], 'active-cover-letter.txt', 'text/plain',
                control_bytes, document_kind='COVER_LETTER',
                label='Archive recovery control',
            )
            self.assertEqual(archived_revision['version'], 2)
            self.assertNotEqual(archived_revision['revision_id'], active_control['revision_id'])
            before_digest = hashlib.sha256(original_bytes).hexdigest()
            app = create_app(
                store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0',
                operation_mode='PERSONAL',
            )
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated document archive API did not start')
                    time.sleep(0.02)

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
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until='domcontentloaded')
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()

                    archive = page.get_by_role('region', name='Document archive', exact=True)
                    prior_name = 'cover letter: Archive recovery target · version 1'
                    target_name = 'cover letter: Archive recovery target · version 2'
                    control_name = 'cover letter: Archive recovery control · version 1'
                    prior_target = archive.get_by_role('article', name=prior_name, exact=True)
                    target = archive.get_by_role('article', name=target_name, exact=True)
                    control = archive.get_by_role('article', name=control_name, exact=True)
                    expect(prior_target.get_by_text('Active', exact=True)).to_be_visible()
                    expect(target.get_by_text('Active', exact=True)).to_be_visible()
                    expect(control.get_by_text('Active', exact=True)).to_be_visible()

                    def download_revision(revision_id):
                        response = context.request.get(
                            f'{origin}/profiles/fixture/document-library/{revision_id}',
                        )
                        self.assertEqual(response.status, 200, response.text())
                        return response.body()

                    self.assertEqual(download_revision(archived_revision['revision_id']), original_bytes)
                    self.assertEqual(hashlib.sha256(
                        download_revision(archived_revision['revision_id']),
                    ).hexdigest(), before_digest)

                    archive_url = (
                        f"**/profiles/fixture/document-library/{archived_revision['revision_id']}/archive"
                    )
                    with page.expect_response(
                        lambda response: response.request.method == 'POST'
                        and response.url.endswith(
                            f"/document-library/{archived_revision['revision_id']}/archive"
                        ),
                    ) as archive_response:
                        target.get_by_role(
                            'button',
                            name='Archive cover letter Archive recovery target · version 2 for profile fixture',
                            exact=True,
                        ).click()
                    self.assertEqual(archive_response.value.status, 200, archive_response.value.text())
                    expect(target.get_by_text('Archived', exact=True)).to_be_visible()
                    expect(prior_target.get_by_text('Active', exact=True)).to_be_visible()
                    expect(control.get_by_text('Active', exact=True)).to_be_visible()
                    active_rows = context.request.get(
                        f'{origin}/profiles/fixture/document-library',
                    ).json()['uploaded']
                    active_ids = {row['revision_id'] for row in active_rows}
                    self.assertNotIn(archived_revision['revision_id'], active_ids)
                    self.assertIn(prior_revision['revision_id'], active_ids)
                    self.assertIn(active_control['revision_id'], active_ids)
                    self.assertEqual(download_revision(archived_revision['revision_id']), original_bytes)

                    # A failed request must preserve active state, show the error, and not retry.
                    failed_archive_url = (
                        f"**/profiles/fixture/document-library/{active_control['revision_id']}/archive"
                    )
                    failed_archive_attempts = []

                    def fail_archive(route):
                        failed_archive_attempts.append(route.request.url)
                        route.fulfill(
                            status=503, content_type='application/json',
                            body='{"detail":"Synthetic archive failure"}',
                        )

                    page.route(failed_archive_url, fail_archive)
                    with page.expect_response(
                        lambda response: response.request.method == 'POST'
                        and response.url.endswith(
                            f"/document-library/{active_control['revision_id']}/archive"
                        ),
                    ) as failed_response:
                        control.get_by_role(
                            'button',
                            name='Archive cover letter Archive recovery control · version 1 for profile fixture',
                            exact=True,
                        ).click()
                    self.assertEqual(failed_response.value.status, 503)
                    self.assertEqual(len(failed_archive_attempts), 1)
                    expect(page.get_by_role('alert')).to_contain_text('Synthetic archive failure')
                    expect(control.get_by_text('Active', exact=True)).to_be_visible()
                    expect(control.get_by_role(
                        'button',
                        name='Archive cover letter Archive recovery control · version 1 for profile fixture',
                        exact=True,
                    )).to_be_visible()
                    active_rows = context.request.get(
                        f'{origin}/profiles/fixture/document-library',
                    ).json()['uploaded']
                    self.assertIn(active_control['revision_id'], {
                        row['revision_id'] for row in active_rows
                    })
                    page.unroute(failed_archive_url)

                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    archive = page.get_by_role('region', name='Document archive', exact=True)
                    target = archive.get_by_role('article', name=target_name, exact=True)
                    prior_target = archive.get_by_role('article', name=prior_name, exact=True)
                    control = archive.get_by_role('article', name=control_name, exact=True)
                    expect(target.get_by_text('Archived', exact=True)).to_be_visible()
                    expect(prior_target.get_by_text('Active', exact=True)).to_be_visible()
                    expect(control.get_by_text('Active', exact=True)).to_be_visible()
                    self.assertEqual(download_revision(archived_revision['revision_id']), original_bytes)

                    with page.expect_response(
                        lambda response: response.request.method == 'POST'
                        and response.url.endswith(
                            f"/document-library/{archived_revision['revision_id']}/restore"
                        ),
                    ) as restore_response:
                        target.get_by_role(
                            'button',
                            name='Restore cover letter Archive recovery target · version 2 for profile fixture',
                            exact=True,
                        ).click()
                    self.assertEqual(restore_response.value.status, 200, restore_response.value.text())
                    expect(target.get_by_text('Active', exact=True)).to_be_visible()
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Profile', exact=True).click()
                    archive = page.get_by_role('region', name='Document archive', exact=True)
                    restored = archive.get_by_role('article', name=target_name, exact=True)
                    expect(restored.get_by_text('Active', exact=True)).to_be_visible()
                    expect(archive.get_by_role(
                        'article', name=prior_name, exact=True,
                    ).get_by_text('Active', exact=True)).to_be_visible()
                    restored_bytes = download_revision(archived_revision['revision_id'])
                    self.assertEqual(restored_bytes, original_bytes)
                    self.assertEqual(hashlib.sha256(restored_bytes).hexdigest(), before_digest)
                    self.assertFalse(external)
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), 'Isolated document archive API must stop')


if __name__ == '__main__':
    unittest.main()
