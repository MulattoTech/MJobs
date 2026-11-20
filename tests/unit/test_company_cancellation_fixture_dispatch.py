"""Cancellation loads fixture receipt checks only for explicitly marked work."""
import builtins
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, now
from workers.operations import company_refresh_saved, request_cancel


BRIDGE = 'services.company_discovery.fixture_monitoring'
MARKER = 'direct-company-source-v1'


class CompanyCancellationFixtureDispatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='mjobs-cancel-dispatch-')
        self.addCleanup(directory.cleanup)
        self.store = Store('sqlite:///' + (Path(directory.name) / 'control.db').as_posix())
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()

    def operation(self, params, kind='COMPANY_DISCOVER'):
        operation = {'id': uuid4().hex, 'entity_id': 'owned-source', 'kind': kind,
            'params': params, 'state': 'RUNNING', 'attempt_count': 1, 'updated_at': now()}
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(**operation))
        return operation

    def forbid_fixture_import(self, name, *args, **kwargs):
        if name == BRIDGE:
            raise AssertionError('Ordinary cancellation loaded the fixture bridge')
        return self.original_import(name, *args, **kwargs)

    def test_ordinary_company_cancellation_does_not_import_fixture_bridge(self):
        self.original_import = builtins.__import__
        for params in ({}, {'fetch_mode': 'PUBLIC_HTTPS'}, {'fixture_source_id': 'owned-source'}):
            with self.subTest(params=params):
                operation = self.operation(params)
                with patch('builtins.__import__', side_effect=self.forbid_fixture_import):
                    result = request_cancel(self.store, operation['id'])
                self.assertTrue(result['cancel_requested'])
                with self.store.engine.connect() as connection:
                    self.assertTrue(connection.execute(select(m.operations.c.cancel_requested)
                        .where(m.operations.c.id == operation['id'])).scalar_one())

    def test_present_malformed_markers_still_delegate_and_cannot_claim_saved_receipt(self):
        original_import = builtins.__import__
        for marker in (None, False, '', 'unsupported-fixture-version'):
            with self.subTest(marker=marker):
                imports = []
                def observe_import(name, *args, **kwargs):
                    if name == BRIDGE:
                        imports.append(name)
                    return original_import(name, *args, **kwargs)
                operation = self.operation({'fixture_monitoring': marker,
                    'fixture_source_id': 'owned-source', 'profile_id': 'owned-profile'})
                with patch('builtins.__import__', side_effect=observe_import):
                    result = request_cancel(self.store, operation['id'])
                self.assertEqual(imports, [BRIDGE])
                self.assertTrue(result['cancel_requested'])
                self.assertNotEqual(result.get('reason'), 'REFRESH_ALREADY_SAVED')

    def test_exact_fixture_receipt_still_prevents_late_cancellation(self):
        params = {'fixture_monitoring': MARKER, 'fixture_source_id': 'owned-source',
            'profile_id': 'owned-profile'}
        operation = self.operation(params)
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        with self.store.engine.begin() as connection:
            self.store._audit(connection, operation['entity_id'], 'FIXTURE_COMPANY_SOURCE_REFRESHED',
                {'operation_id': operation['id'], 'profile_id': params['profile_id'],
                 'source_id': operation['entity_id'], 'params_sha256': digest})
            self.assertTrue(company_refresh_saved(connection, operation))
            for altered in ({'id': 'different-operation'},
                            {'params': {**params, 'profile_id': 'different-profile'}},
                            {'params': {**params, 'fixture_monitoring': None}},
                            {'params': {**params, 'fixture_source_id': 'different-source'}}):
                with self.subTest(altered=altered):
                    self.assertFalse(company_refresh_saved(connection, {**operation, **altered}))
        result = request_cancel(self.store, operation['id'])
        self.assertEqual(result['reason'], 'REFRESH_ALREADY_SAVED')
        self.assertFalse(result['cancel_requested'])

    def test_public_source_refresh_receipt_uses_its_existing_lookup(self):
        self.original_import = builtins.__import__
        operation = self.operation({}, kind='COMPANY_SOURCE_REFRESH')
        with self.store.engine.begin() as connection:
            self.store._audit(connection, operation['entity_id'], 'COMPANY_SOURCE_REFRESHED',
                {'operation_id': operation['id'], 'company_source_id': operation['entity_id']})
        with patch('builtins.__import__', side_effect=self.forbid_fixture_import):
            result = request_cancel(self.store, operation['id'])
        self.assertEqual(result['reason'], 'REFRESH_ALREADY_SAVED')
        self.assertFalse(result['cancel_requested'])
