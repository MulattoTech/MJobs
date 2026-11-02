"""Actual owned Chromium failures exercise all supported diagnostic consumers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from playwright.sync_api import BrowserType, Locator, Page
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from services.applications import controlled_dry_run, manual_auth
from services.discovery.pipeline import MockClient
from tests.integration import test_controlled_application_dry_run as flow


PRIVATE = 'SYNTHETIC_PRIVATE_DIAGNOSTIC_CANARY'


def read_package(test, root, *, phase):
    packages = list(root.glob('*/metadata.json'))
    test.assertEqual(len(packages), 1)
    path = packages[0].parent
    metadata = json.loads(packages[0].read_text())
    dom = json.loads((path / 'dom.json').read_text())
    test.assertEqual(metadata['current_step'], phase)
    test.assertEqual(metadata['screenshot']['status'], 'CAPTURED')
    test.assertEqual(dom['status'], 'CAPTURED')
    test.assertLessEqual((path / 'dom.json').stat().st_size, 8192)
    test.assertNotIn(PRIVATE, json.dumps([metadata, dom]))
    test.assertNotIn('?', metadata['current_url'])
    test.assertEqual(metadata['retention_days'], 30)
    test.assertEqual(metadata['max_captures_per_application'], 20)
    return path, metadata


class ControlledDebugCaptureTests(unittest.TestCase):
    _new_store = flow.ControlledApplicationDryRunTests._new_store
    setUp = flow.ControlledApplicationDryRunTests.setUp
    tearDown = flow.ControlledApplicationDryRunTests.tearDown
    path = flow.ControlledApplicationDryRunTests.path
    execute = flow.ControlledApplicationDryRunTests.execute
    review_and_queue = flow.ControlledApplicationDryRunTests.review_and_queue

    def fail_fill(self, locator, value, *args, **kwargs):
        self.original_fill(locator, value, *args, **kwargs)
        locator.evaluate('(el, value) => { el.value=value; console.error(value); }', PRIVATE)
        raise RuntimeError(PRIVATE)

    def test_all_controlled_providers_capture_failure_and_preserve_durable_result(self):
        providers = (
            ('greenhouse', 'https://boards.greenhouse.io/example/jobs/12345'),
            ('lever', 'https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef'),
            ('workday', 'https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R12345'),
        )
        self.original_fill = Locator.fill
        for provider, url in providers:
            with self.subTest(provider=provider):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.store.application(self.application_id)['route_id']
                    ).values(provider=provider, tenant='acme', requisition='R12345', url=url))
                before = self.store.application(self.application_id)
                _, body, queued = self.review_and_queue('debug-' + provider)
                capture_root = self.root / self.application_id / 'debug-captures'
                previous = set(capture_root.glob('*')) if capture_root.exists() else set()
                with patch.dict('os.environ', {'MJOBS_BROWSER_DEBUG_CAPTURE': '1'}), \
                        patch.object(Locator, 'fill', lambda locator, *a, **kw: self.fail_fill(locator, *a, **kw)):
                    result = self.execute(queued['operation_id'])
                self.assertEqual(result['status'], 'BLOCKED', result)
                self.client.close()
                self.store.engine.dispose()
                self.store = self._new_store()
                self.client = TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root,
                    'redis://127.0.0.1:1/0', operation_mode='FIXTURE'))
                saved = self.client.get(self.path() + '/runs/' + body['request_id']).json()
                self.assertEqual(saved['error_code'], 'CONTROLLED_DRY_RUN_BROWSER_FAILED')
                self.assertIsNone(saved['receipt'])
                self.assertEqual(self.store.application(self.application_id), before)
                self.assertEqual(self.store.count('application_attempts'), 0)
                created = set(capture_root.glob('*')) - previous
                self.assertEqual(len(created), 1)
                package = next(iter(created))
                metadata = json.loads((package / 'metadata.json').read_text())
                dom = json.loads((package / 'dom.json').read_text())
                self.assertEqual(metadata['current_step'], 'FILL_CONTACT')
                self.assertEqual(metadata['screenshot']['status'], 'CAPTURED')
                self.assertEqual(dom['status'], 'CAPTURED')
                self.assertTrue(any(event['source'] == 'console' for event in metadata['diagnostics']))
                self.assertNotIn(PRIVATE, json.dumps([metadata, dom, saved]))

    def test_disabled_capture_creates_no_failure_package(self):
        self.original_fill = Locator.fill
        _, _, queued = self.review_and_queue('disabled-failure')
        with patch.dict('os.environ', {'MJOBS_BROWSER_DEBUG_CAPTURE': '0'}), \
                patch.object(Locator, 'fill', lambda locator, *a, **kw: self.fail_fill(locator, *a, **kw)):
            self.assertEqual(self.execute(queued['operation_id'])['status'], 'BLOCKED')
        self.assertFalse((self.root / self.application_id / 'debug-captures').exists())

    def test_success_with_capture_enabled_creates_no_failure_package(self):
        _, _, success = self.review_and_queue('enabled-success')
        with patch.dict('os.environ', {'MJOBS_BROWSER_DEBUG_CAPTURE': '1'}):
            self.assertEqual(self.execute(success['operation_id'])['status'], 'COMPLETED')
        self.assertFalse((self.root / self.application_id / 'debug-captures').exists())

    def test_diagnostic_failure_cannot_replace_original_browser_failure(self):
        self.original_fill = Locator.fill
        _, body, queued = self.review_and_queue('diagnostic-fails')
        with patch.dict('os.environ', {'MJOBS_BROWSER_DEBUG_CAPTURE': '1'}), \
                patch.object(Locator, 'fill', lambda locator, *a, **kw: self.fail_fill(locator, *a, **kw)), \
                patch.object(controlled_dry_run, 'capture_browser_failure', side_effect=ValueError('secondary')):
            self.assertEqual(self.execute(queued['operation_id'])['status'], 'BLOCKED')
        saved = self.client.get(self.path() + '/runs/' + body['request_id']).json()
        self.assertEqual(saved['error_code'], 'CONTROLLED_DRY_RUN_BROWSER_FAILED')
        self.assertIsNone(saved['receipt'])


class ManualAuthDebugCaptureTests(unittest.TestCase):
    def exercise(self, root, origin, *, enabled, fail):
        profile = root / 'app' / 'browser-profile'
        profile.mkdir(parents=True, exist_ok=True)
        launch = BrowserType.launch_persistent_context
        goto = Page.goto
        wait = Page.wait_for_timeout
        self.box = None

        def headless(browser_type, *args, **kwargs):
            kwargs['headless'] = True
            return launch(browser_type, *args, **kwargs)

        def navigation(page, *args, **kwargs):
            result = goto(page, *args, **kwargs)
            page.locator('[name=password]').fill(PRIVATE)
            self.box = page.locator('[name=password]').bounding_box()
            page.evaluate('(value) => console.error(value)', PRIVATE)
            # The application-owned route boundary aborts this request locally.
            page.evaluate("value => fetch('http://127.0.0.1:1/debug-network-failure?' + value).catch(() => null)", PRIVATE)
            if fail:
                raise RuntimeError(PRIVATE)
            return result

        def user_close(page, milliseconds):
            page.close()
            return wait(page, milliseconds)

        with patch.object(BrowserType, 'launch_persistent_context', headless), \
                patch.object(Page, 'goto', navigation), patch.object(Page, 'wait_for_timeout', user_close):
            return manual_auth.run_browser(profile, origin + '/ats/login/A', MockClient(origin),
                                           debug_capture=enabled)

    def test_failure_capture_has_real_console_network_events_and_masks_credentials(self):
        with tempfile.TemporaryDirectory() as temporary, fixture_server() as origin:
            root = Path(temporary)
            self.assertEqual(self.exercise(root, origin, enabled=True, fail=True), manual_auth.OPEN_UNAVAILABLE)
            package, metadata = read_package(self, root / 'app/debug-captures', phase='MANUAL_AUTH_NAVIGATE')
            sources = {event['source'] for event in metadata['diagnostics']}
            self.assertIn('console', sources)
            self.assertIn('requestfailed', sources)
            box = self.box
            with Image.open(package / 'screenshot.png') as image:
                region = image.convert('RGB').crop((int(box['x']) + 1, int(box['y']) + 1,
                    int(box['x'] + box['width']) - 1, int(box['y'] + box['height']) - 1))
                self.assertEqual(region.getextrema(), ((0, 0), (0, 0), (0, 0)))

    def test_disabled_failure_and_normal_close_do_not_capture_sessions(self):
        for enabled, fail, expected in ((False, True, manual_auth.OPEN_UNAVAILABLE),
                                        (True, False, manual_auth.MANUAL_AUTH_WINDOW_CLOSED)):
            with self.subTest(enabled=enabled, fail=fail), tempfile.TemporaryDirectory() as temporary, fixture_server() as origin:
                root = Path(temporary)
                self.assertEqual(self.exercise(root, origin, enabled=enabled, fail=fail), expected)
                self.assertFalse((root / 'app/debug-captures').exists())


if __name__ == '__main__':
    unittest.main()
