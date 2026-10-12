"""Actual fixture profile, private host, retained observer and original admission."""
import ctypes as c
from ctypes import wintypes as w
from hashlib import sha256
import sys
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from services.applications.browser_tree import BrowserTreeJob
from services.applications.manual_auth_isolated import open_isolated_manual_auth_window
from services.operations.progress import operation_public_record
from tests.integration import test_manual_auth_session as sessions
from workers.operations import admit_inline, OperationConcurrencyLimit


@unittest.skipUnless(sys.platform == 'win32', 'Native isolated Windows host')
class ManualAuthIsolatedBrowserTests(unittest.TestCase):
    setUp = sessions.ManualAuthSessionApiTests.setUp
    tearDown = sessions.ManualAuthSessionApiTests.tearDown

    def test_saved_attempt_private_host_observer_closure_and_held_capacity(self):
        application = self.store.application(self.application_id)
        attempts = self.store.count('application_attempts')
        with self.store.engine.connect() as connection:
            attempt = connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == self.application_id).order_by(
                    m.attempts.c.started_at.desc(), m.attempts.c.id.desc())).mappings().first()
        token = 'a' * 64
        operation = admit_inline(self.store, 'MANUAL_AUTH', self.application_id,
            owner_token=token, manual_auth_identity={'profile_id': 'fixture', 'attempt_id': attempt['id']})
        job = BrowserTreeJob()
        query = job.query_handle()
        posted = []
        done = threading.Event()
        user = c.WinDLL('user32', use_last_error=True)
        callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        user.EnumWindows.argtypes = [callback_type, w.LPARAM]
        user.EnumWindows.restype = w.BOOL
        user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
        user.GetWindowThreadProcessId.restype = w.DWORD
        user.IsWindowVisible.argtypes = [w.HWND]
        user.IsWindowVisible.restype = w.BOOL
        user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.PostMessageW.restype = w.BOOL

        @callback_type
        def close_owned(hwnd, param):
            pid = w.DWORD()
            user.GetWindowThreadProcessId(hwnd, c.byref(pid))
            if user.IsWindowVisible(hwnd) and query.contains(pid.value) is True:
                if user.PostMessageW(hwnd, 0x10, 0, 0):
                    posted.append(pid.value)
            return True

        def stimulus():
            deadline = time.monotonic() + 20
            while not done.is_set() and time.monotonic() < deadline:
                accounting = query.accounting()
                if accounting and accounting['active'] >= 4:
                    # Delay is a fixture stimulus only. Neither elapsed time nor
                    # the close message certifies production browser closure.
                    if not done.wait(2):
                        user.EnumWindows(close_owned, 0)
                    if posted:
                        return
                done.wait(.05)

        closer = threading.Thread(target=stimulus, daemon=True)
        closer.start()
        try:
            with patch('services.applications.manual_auth_isolated.BrowserTreeJob', return_value=job):
                status = open_isolated_manual_auth_window(store=self.store, operation_id=operation,
                    application_id=self.application_id, attempt_id=attempt['id'],
                    mock_origin=self.origin, output_dir=self.root, owner_token=token, budget_seconds=20)
            self.assertEqual(status, 'MANUAL_AUTH_WINDOW_CLOSED')
            self.assertTrue(posted, 'Owned window close stimulus was not delivered')
            with self.store.engine.connect() as connection:
                row = dict(connection.execute(select(m.operations).where(
                    m.operations.c.id == operation)).mappings().one())
            owner = row['recovery_metadata']['manual_auth_owner']
            certificate = owner['tree_closure']
            self.assertEqual(certificate['binding'], {'operation_id': operation,
                'application_id': self.application_id, 'attempt_id': attempt['id'],
                'profile_id': 'fixture', 'owner_digest': sha256(token.encode()).hexdigest()})
            self.assertEqual(certificate['observer'], owner['tree_observer'])
            self.assertGreaterEqual(certificate['accounting']['total'], 4)
            self.assertEqual(certificate['accounting']['active'], 0)
            self.assertEqual(certificate['accounting']['terminated'], 0)
            self.assertEqual(row['state'], 'RUNNING')
            self.assertNotIn('tree_closure', str(operation_public_record(row)))
            self.assertEqual(self.store.application(self.application_id), application)
            self.assertEqual(self.store.count('application_attempts'), attempts)
            with self.assertRaises(OperationConcurrencyLimit):
                admit_inline(self.store, 'MANUAL_AUTH', 'another-app')
            self.assertEqual(httpx.get(self.origin + '/_test/request-counts').json(), {'review': 0, 'submit': 0})
        finally:
            done.set()
            closer.join(5)
            query.close()
            job.close()
