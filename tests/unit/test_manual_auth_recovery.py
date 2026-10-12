"""Only exact durable native closure can recover a departed browser owner."""
from copy import deepcopy
import importlib
import unittest
from unittest.mock import patch

from sqlalchemy import select, update
from packages.persistence import models as m
from tests.unit import test_browser_tree_certificate as fixtures
from workers.operations import finish_inline


class ManualAuthRecoveryProofTests(unittest.TestCase):
    setUp = fixtures.BrowserTreeCertificateTests.setUp
    tearDown = fixtures.BrowserTreeCertificateTests.tearDown
    row = fixtures.BrowserTreeCertificateTests.row
    enroll = fixtures.BrowserTreeCertificateTests.enroll
    persist = fixtures.BrowserTreeCertificateTests.persist
    module = fixtures.BrowserTreeCertificateTests.module

    def recovery(self):
        try:
            return importlib.import_module('services.applications.manual_auth_recovery')
        except ModuleNotFoundError:
            self.fail('Durable original-scope manual browser recovery is missing')

    def closed(self):
        self.assertTrue(self.enroll())
        self.assertTrue(self.persist())
        row = self.row()
        row['recovery_metadata']['manual_auth_owner']['tree_retention'] = 'd' * 64
        with self.store.engine.begin() as c:
            c.execute(update(m.operations).where(m.operations.c.id == self.operation).values(
                recovery_metadata=row['recovery_metadata']))
        return self.row()

    def test_valid_durable_closure_is_exactly_bound(self):
        row = self.closed()
        module = self.recovery()
        self.assertTrue(module.confirmed_tree_closure(row))
        for key in ('operation_id', 'application_id', 'attempt_id', 'profile_id', 'owner_digest'):
            invalid = deepcopy(row)
            invalid['recovery_metadata']['manual_auth_owner']['tree_closure']['binding'][key] = 'other'
            with self.subTest(key=key):
                self.assertFalse(module.confirmed_tree_closure(invalid))

    def test_unknown_live_terminated_legacy_or_changed_observer_never_proves_closure(self):
        row = self.closed()
        changes = [('tree_retention', None), ('tree_observer', {}), ('tree_closure', {}),
                   ('api_process_identified', False)]
        for key, value in changes:
            invalid = deepcopy(row)
            invalid['recovery_metadata']['manual_auth_owner'][key] = value
            with self.subTest(key=key):
                self.assertFalse(self.recovery().confirmed_tree_closure(invalid))
        for accounting in ({'total': 0, 'active': 0, 'terminated': 0},
                           {'total': 4, 'active': 1, 'terminated': 0},
                           {'total': 4, 'active': 0, 'terminated': 1},
                           {'total': True, 'active': 0, 'terminated': 0}):
            invalid = deepcopy(row)
            invalid['recovery_metadata']['manual_auth_owner']['tree_closure']['accounting'] = accounting
            with self.subTest(accounting=accounting):
                self.assertFalse(self.recovery().confirmed_tree_closure(invalid))

    def test_owner_finish_cannot_release_enrolled_tree_without_durable_closure(self):
        self.assertTrue(self.enroll())
        self.assertFalse(finish_inline(self.store, self.operation, 'COMPLETED',
                                      browser_closed=True, owner_token=self.token))
        self.assertEqual(self.row()['state'], 'BLOCKED')
