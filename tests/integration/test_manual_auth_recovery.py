"""Explicit API recovery releases proven departed ownership without replay."""
from hashlib import sha256
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import select, update
from packages.persistence import models as m
from packages.persistence.store import now
from tests.integration import test_manual_auth_session as sessions
from workers.operations import admit_inline


class ManualAuthRecoveryApiTests(unittest.TestCase):
    setUp = sessions.ManualAuthSessionApiTests.setUp
    tearDown = sessions.ManualAuthSessionApiTests.tearDown

    def test_old_held_browser_remains_visible_beyond_recent_operations(self):
        from sqlalchemy import insert
        operation = self.held()
        with self.store.engine.begin() as c:
            template = dict(c.execute(select(m.operations).where(m.operations.c.id == operation)).mappings().one())
            c.execute(update(m.operations).where(m.operations.c.id == operation).values(updated_at='2000-01-01T00:00:00+00:00'))
            for index in range(105):
                c.execute(insert(m.operations).values(**{**template, 'id': f'recent-{index}',
                    'kind': 'DISCOVER', 'entity_id': 'fixture', 'state': 'COMPLETED',
                    'error': None, 'recovery_metadata': {}, 'updated_at': now()}))
        response = self.client.get('/operations?profile_id=fixture')
        self.assertEqual(response.status_code, 200, response.text)
        operations = response.json()['operations']
        self.assertIn(operation, [item['id'] for item in operations])
        self.assertEqual(len({item['id'] for item in operations}), len(operations))
        self.assertNotIn('tree_retention', response.text)
        foreign = self.client.get('/operations?profile_id=other').json()['operations']
        self.assertNotIn(operation, [item['id'] for item in foreign])

    def held(self, certificate=True):
        with self.store.engine.connect() as c:
            attempt = c.execute(select(m.attempts).where(
                m.attempts.c.application_id == self.application_id).order_by(
                    m.attempts.c.started_at.desc(), m.attempts.c.id.desc())).mappings().first()
        token = 'a' * 64
        operation = admit_inline(self.store, 'MANUAL_AUTH', self.application_id,
            owner_token=token, manual_auth_identity={'profile_id': 'fixture', 'attempt_id': attempt['id']})
        with self.store.engine.begin() as c:
            row = c.execute(select(m.operations).where(m.operations.c.id == operation)).mappings().one()
            metadata = row['recovery_metadata']
            owner = metadata['manual_auth_owner']
            observer = {**owner['api_process'], 'pid': 1234, 'created': '1234'}
            owner.update(tree_observer=observer, tree_retention='d' * 64)
            if certificate:
                owner['tree_closure'] = {'version': 1, 'binding': {
                    'operation_id': operation, 'application_id': self.application_id,
                    'attempt_id': attempt['id'], 'profile_id': 'fixture',
                    'owner_digest': sha256(token.encode()).hexdigest()},
                    'observer': observer, 'accounting': {'total': 7, 'active': 0, 'terminated': 0},
                    'observed_at': now()}
            c.execute(update(m.operations).where(m.operations.c.id == operation).values(
                state='BLOCKED', error='BROWSER_RELEASE_UNCONFIRMED', recovery_metadata=metadata))
        return operation

    def recover_url(self, operation, profile='fixture'):
        return f'/profiles/{profile}/applications/{self.application_id}/manual-auth/{operation}/recover'

    def test_recovery_is_explicit_idempotent_and_never_replays_application(self):
        operation = self.held()
        before = self.store.application(self.application_id)
        attempts = self.store.count('application_attempts')
        with patch('services.applications.browser_process.owner_process_status', return_value='EXITED'), \
                patch('apps.api.manual_auth.open_mock_authentication') as launch:
            first = self.client.post(self.recover_url(operation))
            second = self.client.post(self.recover_url(operation))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(first.json()['status'], 'BROWSER_CAPACITY_RECOVERED')
        launch.assert_not_called()
        self.assertEqual(self.store.application(self.application_id), before)
        self.assertEqual(self.store.count('application_attempts'), attempts)
        with self.store.engine.connect() as c:
            row = c.execute(select(m.operations).where(m.operations.c.id == operation)).mappings().one()
            events = c.execute(select(m.audit).where(m.audit.c.entity_id == operation,
                m.audit.c.action == 'MANUAL_AUTH_BROWSER_RECOVERED')).mappings().all()
        self.assertEqual(row['state'], 'FAILED')
        self.assertEqual(row['error'], 'BROWSER_OWNER_RECOVERED')
        self.assertEqual(len(events), 1)
        self.assertNotIn('tree_closure', first.text)
        self.assertNotIn('owner_digest', first.text)

    def test_live_owner_or_missing_certificate_keeps_capacity(self):
        operation = self.held(certificate=False)
        response = self.client.post(self.recover_url(operation))
        self.assertEqual(response.status_code, 409, response.text)
        with self.store.engine.connect() as c:
            self.assertEqual(c.execute(select(m.operations.c.state).where(
                m.operations.c.id == operation)).scalar_one(), 'BLOCKED')

    def test_closed_tree_with_live_or_unknown_owner_is_not_recovered(self):
        operation = self.held()
        for status in ('LIVE', 'UNKNOWN'):
            with self.subTest(status=status), patch(
                    'services.applications.browser_process.owner_process_status', return_value=status):
                response = self.client.post(self.recover_url(operation))
                self.assertEqual(response.status_code, 409, response.text)

    def test_foreign_profile_cannot_recover_original_scope(self):
        operation = self.held()
        response = self.client.post(self.recover_url(operation, 'other'))
        self.assertEqual(response.status_code, 404, response.text)

    def test_changed_attempt_state_keeps_original_capacity(self):
        operation = self.held()
        with self.store.engine.begin() as c:
            c.execute(update(m.attempts).where(m.attempts.c.application_id == self.application_id).values(
                state='REVIEW_REQUIRED'))
        with patch('services.applications.browser_process.owner_process_status', return_value='EXITED'):
            response = self.client.post(self.recover_url(operation))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['code'], 'BROWSER_RECOVERY_SCOPE_CHANGED')

    def test_audit_failure_rolls_back_capacity_release(self):
        operation = self.held()
        with patch('services.applications.browser_process.owner_process_status', return_value='EXITED'), \
                patch.object(self.store, '_audit', side_effect=RuntimeError('Synthetic storage failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.recover_url(operation))
        with self.store.engine.connect() as c:
            self.assertEqual(c.execute(select(m.operations.c.state).where(
                m.operations.c.id == operation)).scalar_one(), 'BLOCKED')

    def test_concurrent_explicit_recovery_has_one_audit_and_no_replay(self):
        from concurrent.futures import ThreadPoolExecutor
        operation = self.held()
        with patch('services.applications.browser_process.owner_process_status', return_value='EXITED'), \
                patch('apps.api.manual_auth.open_mock_authentication') as launch:
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: self.client.post(self.recover_url(operation)), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        launch.assert_not_called()
        with self.store.engine.connect() as c:
            events = c.execute(select(m.audit.c.id).where(m.audit.c.entity_id == operation,
                m.audit.c.action == 'MANUAL_AUTH_BROWSER_RECOVERED')).all()
        self.assertEqual(len(events), 1)

    @unittest.skipUnless(sys.platform == 'win32', 'Native isolated Windows host')
    def test_mounted_sign_in_passes_original_admission_to_isolated_host(self):
        with patch('services.applications.mock_adapter.open_mock_authentication',
                   return_value='MANUAL_AUTH_WINDOW_CLOSED') as legacy, \
                patch('services.applications.manual_auth_isolated.open_isolated_manual_auth_window',
                      return_value='MANUAL_AUTH_WINDOW_CLOSED') as isolated:
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        isolated.assert_called_once()
        legacy.assert_not_called()
        kwargs = isolated.call_args.kwargs
        self.assertEqual(kwargs['application_id'], self.application_id)
        self.assertEqual(len(kwargs['owner_token']), 64)
        with self.store.engine.connect() as c:
            row = c.execute(select(m.operations).where(
                m.operations.c.id == kwargs['operation_id'])).mappings().one()
        self.assertEqual(row['entity_id'], self.application_id)
        self.assertEqual(row['recovery_metadata']['manual_auth_owner']['attempt_id'], kwargs['attempt_id'])
