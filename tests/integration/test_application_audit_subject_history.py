"""Application Activity retains transaction-time labels and ownership."""
import os
import unittest
from unittest.mock import patch

from sqlalchemy import delete, select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.audit_history import record_mock_application_submitted
from services.tracking.applications import add_application_note, set_application_pipeline_status
from tests.integration import test_document_generation_audit as fixture
from tests.integration import test_lever_form_review_flow as postgres_fixture


class ApplicationAuditSubjectHistoryTests(unittest.TestCase):
    _new_store = fixture.DocumentGenerationAuditTests._new_store
    setUp = fixture.DocumentGenerationAuditTests.setUp
    _client = fixture.DocumentGenerationAuditTests._client
    tearDown = fixture.DocumentGenerationAuditTests.tearDown
    history = fixture.DocumentGenerationAuditTests.history

    def application_events(self):
        return [e for e in self.history() if e['type'] in {
            'RESUME_GENERATED', 'COVER_LETTER_GENERATED', 'DOCUMENTS_PREPARED',
            'APPLICATION_SUBMITTED', 'APPLICATION_CONFIRMED',
            'APPLICATION_STATUS_REPORTED', 'APPLICATION_NOTE_ADDED'}]

    def record_events(self):
        record_mock_application_submitted(self.store, self.application_id)
        self.store.transition(self.application_id, ['PREPARED'], 'CONFIRMED')
        set_application_pipeline_status(self.store, 'fixture', self.application_id, 'APPLIED', 'status-1')
        add_application_note(self.store, 'fixture', self.application_id, 'Synthetic private note', 'note-1')

    def test_document_submission_confirmation_and_tracking_survive_rename_replay_and_reopen(self):
        self.record_events()
        before = self.application_events()
        self.assertEqual(len(before), 6)
        with self.store.engine.begin() as c:
            c.execute(update(m.jobs).values(title='Later title'))
            c.execute(update(m.companies).values(name='Later employer'))
            c.execute(update(m.applications).where(m.applications.c.id == self.application_id).values(target='REAL'))
        set_application_pipeline_status(self.store, 'fixture', self.application_id, 'APPLIED', 'status-1')
        add_application_note(self.store, 'fixture', self.application_id, 'Synthetic private note', 'note-1')
        self.assertEqual(self.application_events(), before)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self.application_events(), before)
        self.assertFalse(self.history('other') and any(e['id'] in {e['id'] for e in before}
                                                     for e in self.history('other')))

    def test_application_removal_retains_history_without_exposing_it_to_another_profile(self):
        self.record_events()
        before = self.application_events()
        with self.store.engine.begin() as c:
            c.execute(delete(m.documents).where(m.documents.c.application_id == self.application_id))
            c.execute(delete(m.application_route_choices).where(m.application_route_choices.c.application_id == self.application_id))
            c.execute(delete(m.applications).where(m.applications.c.id == self.application_id))
        self.assertEqual(self.application_events(), before)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self.application_events(), before)
        self.assertFalse(any(e['id'] in {e['id'] for e in before} for e in self.history('other')))

    def test_legacy_application_events_keep_type_time_and_unknown_labels(self):
        self.record_events()
        before = self.application_events()
        with self.store.engine.begin() as c:
            for row in c.execute(select(m.audit)).mappings().all():
                if row['id'] in {e['id'].split(':')[0] for e in before}:
                    payload = dict(row['payload'])
                    payload.pop('subject_snapshot', None)
                    payload.pop('application_context', None)
                    c.execute(update(m.audit).where(m.audit.c.id == row['id']).values(payload=payload))
        after = self.application_events()
        self.assertEqual([(e['id'], e['type'], e['timestamp']) for e in after],
                         [(e['id'], e['type'], e['timestamp']) for e in before])
        self.assertTrue(all(e['subject'] == 'Historical details not recorded' and e['company'] == ''
                            for e in after))

    def test_tracking_post_insert_failure_rolls_back_and_retry_records_once(self):
        from services.tracking import applications as tracking
        before = self.history()
        original = tracking.upsert

        def fail_after_insert(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('Synthetic post-insert rollback')

        with patch.object(tracking, 'upsert', side_effect=fail_after_insert), self.assertRaises(RuntimeError):
            add_application_note(self.store, 'fixture', self.application_id, 'Fixture note', 'rollback-note')
        self.assertEqual(self.history(), before)
        for _ in range(2):
            add_application_note(self.store, 'fixture', self.application_id, 'Fixture note', 'rollback-note')
        added = [e for e in self.history() if e['id'] not in {e['id'] for e in before}]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['type'], 'APPLICATION_NOTE_ADDED')

    def test_real_confirmation_state_does_not_invent_employer_receipt(self):
        with self.store.engine.begin() as c:
            c.execute(update(m.applications).where(m.applications.c.id == self.application_id).values(target='REAL'))
        self.store.transition(self.application_id, ['PREPARED'], 'CONFIRMED')
        self.assertFalse(any(e['type'] == 'APPLICATION_CONFIRMED' for e in self.history()))
        with self.store.engine.begin() as c:
            c.execute(update(m.applications).where(m.applications.c.id == self.application_id).values(target='MOCK'))
        self.assertFalse(any(e['type'] == 'APPLICATION_CONFIRMED' for e in self.history()))

    def test_application_state_lifecycle_keeps_original_labels(self):
        for expected, state in [('PREPARED', 'QUEUED'), ('QUEUED', 'STARTED'), ('STARTED', 'FAILED')]:
            self.store.transition(self.application_id, [expected], state)
        types = {'APPLICATION_QUEUED', 'APPLICATION_STARTED', 'APPLICATION_FAILED'}
        before = [e for e in self.history() if e['type'] in types]
        self.assertEqual({e['type'] for e in before}, types)
        with self.store.engine.begin() as c:
            c.execute(update(m.jobs).values(title='Later failed application'))
            c.execute(update(m.companies).values(name='Later employer'))
        self.assertEqual([e for e in self.history() if e['type'] in types], before)


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'Requires isolated disposable PostgreSQL')
class ApplicationAuditSubjectHistoryPostgresTests(ApplicationAuditSubjectHistoryTests):
    _new_store = postgres_fixture.LeverFormReviewPostgresFlowTests._new_store
    _drop_schema = postgres_fixture.LeverFormReviewPostgresFlowTests._drop_schema
