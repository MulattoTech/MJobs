"""Composition fences the original admission before any isolated host launch."""
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from tests.unit import test_browser_tree_certificate as certificates


class ManualAuthIsolatedTests(unittest.TestCase):
    # Reuse only the disposable persisted admission fixture, not its test suite.
    setUp = certificates.BrowserTreeCertificateTests.setUp
    tearDown = certificates.BrowserTreeCertificateTests.tearDown
    row = certificates.BrowserTreeCertificateTests.row
    def invoke(self, **changes):
        from services.applications import manual_auth_isolated as module
        kwargs = dict(store=self.store, operation_id=self.operation,
                      application_id='synthetic-app', attempt_id='synthetic-attempt',
                      mock_origin='http://127.0.0.1:8123', output_dir=Path(self.directory.name),
                      owner_token=self.token, budget_seconds=30)
        kwargs.update(changes)
        return module.open_isolated_manual_auth_window(**kwargs)

    def setup_transport(self, stack):
        from services.applications import manual_auth_isolated as module
        self.events = []
        self.job = Mock()
        self.query = self.job.query_handle.return_value
        self.query.accounting.return_value = {'total': 4, 'active': 0, 'terminated': 0}
        self.job.launch.side_effect = lambda *a, **kw: self.events.append('launch') or 123
        self.observer = Mock()
        self.observer.observe.return_value = {'binding': self.binding, 'status': 'EXITED',
                                            'accounting': self.query.accounting.return_value}
        def observer(*args, **kwargs):
            self.events.append('enroll')
            self.assertEqual(kwargs, {'store': self.store, 'owner_token': self.token})
            self.assertEqual(args[1], self.binding)
            return self.observer
        stack.enter_context(patch.object(module.sys, 'platform', 'win32'))
        stack.enter_context(patch.object(module, '_resolve_auth_attempt', return_value=(
            {'url': 'http://127.0.0.1:8123/ats/apply/job_1', 'requisition': 'job_1'},
            Path(self.directory.name) / 'synthetic-app/browser-profile')))
        stack.enter_context(patch.object(module, 'BrowserTreeJob', return_value=self.job))
        stack.enter_context(patch.object(module, 'BrowserTreeObserver', side_effect=observer))
        self.exit = self.job.host_exit_code
        self.exit.return_value = 0

    def test_original_scope_enrolls_before_private_launch_and_requires_tree_exit(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.setup_transport(stack)
            self.assertEqual(self.invoke(), 'MANUAL_AUTH_WINDOW_CLOSED')
        self.assertEqual(self.events, ['enroll', 'launch'])
        args, kwargs = self.job.launch.call_args
        payload = json.loads(kwargs['private_input'])
        self.assertEqual(payload['application_id'], 'synthetic-app')
        self.assertEqual(payload['requisition'], 'job_1')
        self.assertNotIn(self.token, str(args))
        self.assertNotIn('synthetic-app', str(args))
        self.assertNotIn('owner_token', payload)
        self.observer.close.assert_called_once()
        self.query.close.assert_called_once()
        self.job.close.assert_called_once()
        self.assertEqual(self.row()['state'], 'RUNNING')

    def test_wrong_owner_attempt_application_or_operation_never_launches(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.setup_transport(stack)
            for changes in ({'owner_token': 'c' * 64}, {'attempt_id': 'wrong'},
                            {'application_id': 'wrong'}, {'operation_id': 'wrong'},
                            {'budget_seconds': True}, {'budget_seconds': 0}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.invoke(**changes), 'STALE')
            self.job.launch.assert_not_called()

    def test_failed_observer_ack_never_launches(self):
        from contextlib import ExitStack
        from services.applications import manual_auth_isolated as module
        with ExitStack() as stack:
            self.setup_transport(stack)
            stack.enter_context(patch.object(module, 'BrowserTreeObserver', side_effect=ValueError('private')))
            self.assertEqual(self.invoke(), 'CLOSE_UNCONFIRMED')
            self.job.launch.assert_not_called()

    def test_zero_tree_does_not_turn_host_failure_or_unknown_into_success(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.setup_transport(stack)
            for code in (2, 3, None):
                self.exit.return_value = code
                with self.subTest(code=code):
                    self.assertEqual(self.invoke(), 'CLOSE_UNCONFIRMED')

    def test_live_unknown_terminated_or_wrong_bound_tree_keeps_capacity(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.setup_transport(stack)
            for reply in ({'binding': self.binding, 'status': 'UNKNOWN', 'accounting': None},
                          {'binding': self.binding, 'status': 'EXITED', 'accounting': {'total': 4, 'active': 0, 'terminated': 1}},
                          {'binding': {**self.binding, 'attempt_id': 'other'}, 'status': 'EXITED', 'accounting': {'total': 4, 'active': 0, 'terminated': 0}}):
                self.observer.observe.return_value = reply
                with self.subTest(reply=reply):
                    self.assertEqual(self.invoke(), 'CLOSE_UNCONFIRMED')
            self.assertEqual(self.row()['state'], 'RUNNING')

    def test_cancel_after_launch_surrenders_channel_without_killing_browser(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.setup_transport(stack)
            calls = []
            def check():
                calls.append(True)
                if len(calls) == 3:
                    raise RuntimeError('cancelled')
            self.assertEqual(self.invoke(check=check), 'CLOSE_UNCONFIRMED')
            self.observer.close.assert_called_once()
            self.assertEqual(self.row()['state'], 'RUNNING')
