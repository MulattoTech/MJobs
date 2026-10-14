"""Application snapshots contain safe labels; malformed ownership fails closed."""
import os
import unittest

from sqlalchemy import select, update

from packages.persistence import models as m
from services.audit_history import record_mock_application_submitted
from services.documents.generate import prepare
from services.tracking.applications import add_application_note, set_application_pipeline_status
from tests.integration import test_document_generation_audit as fixture
from tests.integration import test_lever_form_review_flow as postgres_fixture


class ApplicationAuditSubjectPrivacyTests(unittest.TestCase):
    _new_store = fixture.DocumentGenerationAuditTests._new_store
    setUp = fixture.DocumentGenerationAuditTests.setUp
    _client = fixture.DocumentGenerationAuditTests._client
    tearDown = fixture.DocumentGenerationAuditTests.tearDown
    history = fixture.DocumentGenerationAuditTests.history

    def test_private_labels_are_withheld_before_document_submission_and_tracking_persistence(self):
        with self.store.engine.begin() as c:
            c.execute(update(m.jobs).values(title='Role password=SYNTHETIC_PRIVATE_LABEL'))
            c.execute(update(m.companies).values(name='Employer token=SYNTHETIC_PRIVATE_LABEL'))
        prepare(self.store, self.job_id, 'fixture', self.root, cover_letter_style='warm')
        record_mock_application_submitted(self.store, self.application_id)
        set_application_pipeline_status(self.store, 'fixture', self.application_id, 'APPLIED', 'private-status')
        add_application_note(self.store, 'fixture', self.application_id, 'SYNTHETIC_PRIVATE_NOTE', 'private-note')
        with self.store.engine.connect() as c:
            rows = c.execute(select(m.audit).where(m.audit.c.action.in_(
                ['DOCUMENTS_PREPARED', 'APPLICATION_SUBMITTED', 'APPLICATION_PIPELINE_STATUS', 'APPLICATION_NOTE']))).mappings().all()
        self.assertEqual(len(rows), 5)
        for row in rows:
            with self.subTest(action=row['action']):
                self.assertIn('subject_snapshot', row['payload'])
                self.assertNotIn('SYNTHETIC_PRIVATE', str(row['payload']['subject_snapshot']))
        response = self.client.get('/profiles/fixture/audit-history')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('SYNTHETIC_PRIVATE', response.text)
        self.assertNotIn('application_context', response.text)

    def test_malformed_or_cross_profile_context_cannot_reassign_audit_ownership(self):
        with self.store.engine.connect() as c:
            row = c.execute(select(m.audit).where(m.audit.c.action == 'DOCUMENTS_PREPARED')).mappings().one()
        for context in (None, [], {'profile_id': 'fixture'},
                        {'profile_id': 'other', 'application_id': self.application_id, 'target': 'MOCK'},
                        {'profile_id': 'fixture', 'application_id': 'different', 'target': 'MOCK'},
                        {'profile_id': 'fixture', 'application_id': self.application_id, 'target': 'MOCK', 'secret': 'SYNTHETIC_PRIVATE'}):
            with self.subTest(context=context), self.store.engine.begin() as c:
                c.execute(update(m.audit).where(m.audit.c.id == row['id']).values(payload={
                    **row['payload'], 'application_context': context,
                    'subject_snapshot': {'subject': 'SYNTHETIC_PRIVATE', 'company': ''}}))
            for profile in ('fixture', 'other'):
                events = self.history(profile)
                self.assertFalse(any(e['id'].startswith(row['id']) for e in events))


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'Requires isolated disposable PostgreSQL')
class ApplicationAuditSubjectPrivacyPostgresTests(ApplicationAuditSubjectPrivacyTests):
    _new_store = postgres_fixture.LeverFormReviewPostgresFlowTests._new_store
    _drop_schema = postgres_fixture.LeverFormReviewPostgresFlowTests._drop_schema
