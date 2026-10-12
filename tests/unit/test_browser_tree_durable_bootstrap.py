"""Private bootstrap enrolls before launch and persists after creator death."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from packages.persistence.store import Store
from services.applications.browser_tree import BrowserTreeJob
from services.applications.browser_tree_ipc import BrowserTreeObserver
from services.applications.browser_process import owner_process_status
from services.operations.progress import operation_public_record
from workers.operations import admit_inline, OperationConcurrencyLimit
from tests.unit import test_browser_tree_certificate as certificate_tests
from sqlalchemy import select
from packages.persistence import models as m
from tests.unit.test_browser_tree_containment import eventually


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows durable observer')
class DurableBootstrapTests(unittest.TestCase):
    setUp = certificate_tests.BrowserTreeCertificateTests.setUp
    tearDown = certificate_tests.BrowserTreeCertificateTests.tearDown
    row = certificate_tests.BrowserTreeCertificateTests.row

    def test_enrollment_precedes_ack_and_persists_cooperative_closure(self):
        job = BrowserTreeJob()
        query = job.query_handle()
        observer = None
        location = Path(self.directory.name)
        try:
            observer = BrowserTreeObserver(query, self.binding, store=self.store, owner_token=self.token)
            owner = self.row()['recovery_metadata']['manual_auth_owner']
            self.assertEqual(owner['tree_observer'], observer.identity)
            self.assertEqual(len(owner['tree_retention']), 64)
            self.assertNotIn('tree_closure', owner)
            job.launch([sys.executable, str(Path('tests/fixtures/browser_tree_child.py').resolve()),
                        'root', str(location)])
            eventually(lambda: (location / 'ready.json').exists())
            self.assertEqual(observer.observe()['status'], 'LIVE')
            self.assertNotIn('tree_closure', self.row()['recovery_metadata']['manual_auth_owner'])
            (location / 'release').touch()
            eventually(lambda: observer.observe()['status'] == 'EXITED')
            certificate = self.row()['recovery_metadata']['manual_auth_owner']['tree_closure']
            self.assertEqual(certificate['observer'], observer.identity)
            self.assertEqual(certificate['binding'], self.binding)
            self.assertEqual(self.row()['state'], 'RUNNING')
        finally:
            (location / 'release').touch()
            if observer is not None:
                observer.close()
            query.close()
            job.close()

    def test_wrong_owner_bootstrap_has_no_ack_or_enrollment(self):
        job = BrowserTreeJob()
        query = job.query_handle()
        try:
            with self.assertRaisesRegex(ValueError, 'TREE_OBSERVER_UNAVAILABLE'):
                BrowserTreeObserver(query, self.binding, store=self.store, owner_token='c' * 64)
            self.assertNotIn('tree_observer', self.row()['recovery_metadata']['manual_auth_owner'])
        finally:
            query.close()
            job.close()

    def test_stale_scope_fails_child_enrollment_without_replacing_owner(self):
        job = BrowserTreeJob()
        query = job.query_handle()
        baseline = self.row()
        try:
            for key in ('operation_id', 'application_id', 'attempt_id', 'profile_id'):
                with self.subTest(key=key):
                    with self.assertRaisesRegex(ValueError, 'TREE_OBSERVER_UNAVAILABLE'):
                        BrowserTreeObserver(query, {**self.binding, key: 'stale-synthetic'},
                            store=self.store, owner_token=self.token)
                    self.assertEqual(self.row(), baseline)
        finally:
            query.close()
            job.close()

    def test_lost_ack_rolls_back_only_unlaunched_enrollment_and_allows_retry(self):
        job = BrowserTreeJob()
        query = job.query_handle()
        observer = None
        try:
            with patch('services.applications.browser_tree_ipc._read', return_value={}):
                with self.assertRaisesRegex(ValueError, 'TREE_OBSERVER_UNAVAILABLE'):
                    BrowserTreeObserver(query, self.binding, store=self.store, owner_token=self.token)
            eventually(lambda: 'tree_observer' not in self.row()['recovery_metadata']['manual_auth_owner'])
            self.assertEqual(self.row()['state'], 'RUNNING')
            observer = BrowserTreeObserver(query, self.binding, store=self.store, owner_token=self.token)
            self.assertEqual(self.row()['recovery_metadata']['manual_auth_owner']['tree_observer'], observer.identity)
        finally:
            if observer is not None:
                observer.close()
            query.close()
            job.close()

    def test_rollback_rejects_replacement_query_wrong_token_and_admitted_tree(self):
        from services.applications.browser_tree_certificate import enroll_retained_observer, rollback_unlaunched_enrollment
        job = BrowserTreeJob()
        other_job = BrowserTreeJob()
        query = job.query_handle()
        other_query = other_job.query_handle()
        location = Path(self.directory.name)
        try:
            self.assertTrue(enroll_retained_observer(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            baseline = self.row()
            self.assertFalse(rollback_unlaunched_enrollment(self.store, self.operation, other_query,
                self.binding, owner_token=self.token))
            self.assertFalse(rollback_unlaunched_enrollment(self.store, self.operation, query,
                self.binding, owner_token='c' * 64))
            self.assertEqual(self.row(), baseline)
            job.launch([sys.executable, str(Path('tests/fixtures/browser_tree_child.py').resolve()),
                        'root', str(location)])
            eventually(lambda: (location / 'ready.json').exists())
            self.assertFalse(rollback_unlaunched_enrollment(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            (location / 'release').touch()
            eventually(lambda: query.status() == 'EXITED')
            self.assertFalse(rollback_unlaunched_enrollment(self.store, self.operation, query,
                self.binding, owner_token=self.token))
            self.assertEqual(self.row(), baseline)
        finally:
            (location / 'release').touch()
            query.close()
            other_query.close()
            job.close()
            other_job.close()

    def test_creator_death_retains_live_chromium_then_persists_on_peer_reload(self):
        self.check_creator_death()

    def test_creator_death_recovers_after_sqlite_write_timeout(self):
        self.check_creator_death(write_outage=True)

    def check_creator_death(self, write_outage=False):
        location = Path(self.directory.name)
        creator = subprocess.Popen([sys.executable, str(Path('tests/fixtures/browser_tree_durable_creator.py').resolve())],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        identity = None
        try:
            creator.stdin.write(json.dumps({'directory': str(location), 'url': self.url,
                'token': self.token, 'binding': self.binding}) + '\n')
            creator.stdin.close()
            eventually(lambda: (location / 'observer.json').exists(), 25)
            identity = json.loads((location / 'observer.json').read_text())
            self.assertEqual(creator.wait(timeout=10), 0)
            self.assertEqual(owner_process_status(identity), 'LIVE')
            self.assertNotIn('tree_closure', self.row()['recovery_metadata']['manual_auth_owner'])
            if write_outage:
                # A real lock exceeds the observer's 15-second SQLite write
                # timeout. Reads remain available; no private configuration.
                import sqlite3
                lock = sqlite3.connect(location / 'fixture.db')
                try:
                    lock.execute('BEGIN IMMEDIATE')
                    (location / 'release').touch()
                    host = json.loads((location / 'ready.json').read_text())['host']
                    eventually(lambda: owner_process_status(host) == 'EXITED', 15)
                    time.sleep(17)
                    self.assertEqual(owner_process_status(identity), 'LIVE')
                    self.assertNotIn('tree_closure', self.row()['recovery_metadata']['manual_auth_owner'])
                    self.assertEqual(self.row()['state'], 'RUNNING')
                finally:
                    lock.rollback()
                    lock.close()
            else:
                (location / 'release').touch()
            eventually(lambda: 'tree_closure' in self.row()['recovery_metadata']['manual_auth_owner'], 15)
            peer = Store(self.url)
            try:
                with peer.engine.connect() as connection:
                    row = connection.execute(select(m.operations).where(m.operations.c.id == self.operation)).mappings().one()
                cert = row['recovery_metadata']['manual_auth_owner']['tree_closure']
                self.assertEqual(cert['observer'], identity)
                self.assertEqual(cert['accounting']['active'], 0)
                self.assertEqual(cert['accounting']['terminated'], 0)
                self.assertEqual(row['state'], 'RUNNING')
                self.assertEqual(owner_process_status(row['recovery_metadata']['manual_auth_owner']['api_process']), 'EXITED')
                self.assertNotIn('tree_closure', str(operation_public_record(row)))
                self.assertNotIn(self.token, str(row))
                self.assertNotIn(self.url, str(row))
                with self.assertRaises(OperationConcurrencyLimit):
                    admit_inline(peer, 'MANUAL_AUTH', 'another-app')
            finally:
                peer.engine.dispose()
            eventually(lambda: owner_process_status(identity) == 'EXITED', 10)
        finally:
            (location / 'release').touch()
            creator.wait(timeout=30)
            creator.stderr.close()
            if identity:
                eventually(lambda: owner_process_status(identity) == 'EXITED', 15)


class PersistenceDescriptorTests(unittest.TestCase):
    def test_creator_loss_retries_failed_write_without_dropping_query(self):
        self.run_departed_creator([False, True], expected_writes=2)

    def test_creator_loss_failed_writes_expire_without_success(self):
        self.run_departed_creator(False, expected_writes=None)

    def run_departed_creator(self, writes, expected_writes):
        from hashlib import sha256
        from contextlib import ExitStack
        from services.applications import browser_tree_ipc as ipc
        token = 'a' * 64
        binding = {'operation_id': 'op', 'application_id': 'app',
            'attempt_id': 'attempt', 'profile_id': 'profile',
            'owner_digest': sha256(token.encode()).hexdigest()}
        initial = {'handle': 1, 'nonce': 'a' * 64, 'binding': binding,
            'persistence': {'url': 'sqlite:///synthetic.db', 'owner_token': token}}
        query = Mock()
        query.contains.return_value = False
        query.limit_flags.return_value = 0
        query.accounting.return_value = {'total': 1, 'active': 1, 'terminated': 0}
        query.status.return_value = 'EXITED'
        elapsed = [0.0]
        def sleep(seconds):
            self.assertFalse(query.close.called)
            elapsed[0] += seconds
        def persist(*args, **kwargs):
            self.assertIs(args[2], query)
            self.assertEqual(args[3], binding)
            self.assertEqual(kwargs['owner_token'], token)
            self.assertFalse(query.close.called)
            return writes.pop(0) if isinstance(writes, list) else writes
        with ExitStack() as stack:
            stack.enter_context(patch.object(ipc, '_read', side_effect=[initial, ipc._ChannelLost()]))
            stack.enter_context(patch.object(ipc, '_write'))
            stack.enter_context(patch.object(ipc.time, 'monotonic', side_effect=lambda: elapsed[0]))
            stack.enter_context(patch.object(ipc.time, 'sleep', side_effect=sleep))
            stack.enter_context(patch('services.applications.browser_tree._kernel'))
            stack.enter_context(patch('services.applications.browser_tree.BrowserTreeQuery', return_value=query))
            stack.enter_context(patch('packages.persistence.store.Store'))
            stack.enter_context(patch('services.applications.browser_tree_certificate.enroll_retained_observer', return_value=True))
            stack.enter_context(patch('services.applications.browser_tree_certificate.rollback_unlaunched_enrollment'))
            writer = stack.enter_context(patch('services.applications.browser_tree_certificate.record_retained_closure', side_effect=persist))
            ipc._serve()
        query.close.assert_called_once()
        if expected_writes is not None:
            self.assertEqual(writer.call_count, expected_writes)
            self.assertGreater(elapsed[0], 0)
        else:
            self.assertGreater(writer.call_count, 1)
            self.assertGreaterEqual(elapsed[0], 900)
            self.assertLess(elapsed[0], 902)

    def test_private_descriptor_rejects_malformed_foreign_and_ephemeral_stores(self):
        from hashlib import sha256
        from services.applications.browser_tree_ipc import _validate_persistence
        token = 'a' * 64
        binding = {'owner_digest': sha256(token.encode()).hexdigest()}
        good = {'url': 'sqlite:///synthetic.db', 'owner_token': token}
        self.assertEqual(_validate_persistence(good, binding), good)
        for value in (None, {}, {**good, 'extra': True}, {**good, 'owner_token': 'c' * 64},
                {**good, 'owner_token': 'A' * 64}, {**good, 'url': 'sqlite:///:memory:'},
                {**good, 'url': 'sqlite://'}, {**good, 'url': 'mysql://example/db'},
                {**good, 'url': 'broken synthetic credential'}, {**good, 'url': 'x' * 513}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^TREE_OBSERVER_UNAVAILABLE$'):
                    _validate_persistence(value, binding)
