"""Manual sign-in must bound browser waits by the original operation budget."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from services.applications import manual_auth, manual_auth_host


class ManualAuthDeadlineTests(unittest.TestCase):
    def browser(self, check):
        context = Mock()
        page = Mock()
        page.is_closed.side_effect = [False, True]
        context.pages = [page]
        chromium = Mock()
        chromium.launch_persistent_context.return_value = context
        manager = Mock()
        manager.__enter__ = Mock(return_value=Mock(chromium=chromium))
        manager.__exit__ = Mock(return_value=False)
        client = Mock()
        with patch('playwright.sync_api.sync_playwright', return_value=manager):
            status = manual_auth.run_browser(Path('synthetic-profile'),
                'http://127.0.0.1:8123/ats/login/A', client, check=check)
        return status, chromium, context, page, client

    def test_each_wait_uses_remaining_budget_and_cleanup_still_runs(self):
        check = Mock()
        check.remaining_seconds.side_effect = [1.2, .7, .1]
        status, chromium, context, page, client = self.browser(check)
        self.assertEqual(status, manual_auth.MANUAL_AUTH_WINDOW_CLOSED)
        self.assertEqual(chromium.launch_persistent_context.call_args.kwargs['timeout'], 1200)
        self.assertEqual(page.goto.call_args.kwargs['timeout'], 700)
        page.wait_for_timeout.assert_called_once_with(100)
        context.close.assert_called_once()
        client.close.assert_called_once()

    def test_exhausted_or_invalid_budget_never_launches(self):
        for remaining in (0, -.1, .0001, True, float('nan'), float('inf'), '1'):
            with self.subTest(remaining=remaining):
                check = Mock()
                check.remaining_seconds.return_value = remaining
                status, chromium, context, page, client = self.browser(check)
                self.assertEqual(status, manual_auth.OPEN_UNAVAILABLE)
                chromium.launch_persistent_context.assert_not_called()
                client.close.assert_called_once()

    def test_legacy_check_keeps_finite_action_caps(self):
        def check():
            pass
        status, chromium, context, page, client = self.browser(check)
        self.assertEqual(status, manual_auth.MANUAL_AUTH_WINDOW_CLOSED)
        self.assertEqual(chromium.launch_persistent_context.call_args.kwargs['timeout'], 15000)
        self.assertEqual(page.goto.call_args.kwargs['timeout'], 15000)
        page.wait_for_timeout.assert_called_once_with(250)

    def test_expiration_after_launch_cleans_up_without_success(self):
        for budgets in ([1.0, 0], [1.0, .5, 0]):
            with self.subTest(budgets=budgets):
                check = Mock()
                check.remaining_seconds.side_effect = budgets
                status, chromium, context, page, client = self.browser(check)
                self.assertEqual(status, manual_auth.OPEN_UNAVAILABLE)
                chromium.launch_persistent_context.assert_called_once()
                context.close.assert_called_once()
                client.close.assert_called_once()
                page.wait_for_timeout.assert_not_called()
                if len(budgets) == 2:
                    page.goto.assert_not_called()

    def test_host_exposes_decreasing_original_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'app1/browser-profile').mkdir(parents=True)
            payload = {'schema': 1, 'output_dir': str(root), 'application_id': 'app1',
                'mock_origin': 'http://127.0.0.1:8123', 'requisition': 'A', 'budget_seconds': 1}
            now = [10.0]
            def browser(*args, check):
                self.assertEqual(check.remaining_seconds(), 1.0)
                now[0] = 10.75
                self.assertEqual(check.remaining_seconds(), .25)
                now[0] = 11.0
                with self.assertRaises(TimeoutError):
                    check.remaining_seconds()
                return manual_auth.OPEN_UNAVAILABLE
            with patch.object(manual_auth_host, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(manual_auth_host, 'run_browser', side_effect=browser):
                self.assertEqual(manual_auth_host.run(io.BytesIO(json.dumps(payload).encode())), 2)
