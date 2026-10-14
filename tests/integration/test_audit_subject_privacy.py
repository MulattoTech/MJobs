"""New historical labels are sanitized before durable audit storage and at readback."""
import json
import os
import unittest

from sqlalchemy import delete, select, update

from packages.persistence import models as m
from services.audit_history import record_job_merged
from tests.integration import test_audit_subject_history as fixture
from tests.integration import test_lever_form_review_flow as postgres_fixture


class AuditSubjectPrivacyTests(unittest.TestCase):
    _new_store = fixture.AuditSubjectHistoryTests._new_store
    setUp = fixture.AuditSubjectHistoryTests.setUp
    _client = fixture.AuditSubjectHistoryTests._client
    tearDown = fixture.AuditSubjectHistoryTests.tearDown
    history = fixture.AuditSubjectHistoryTests.history

    def test_private_current_labels_are_not_copied_into_new_audit_payloads(self):
        with self.store.engine.begin() as c:
            company_id = c.execute(select(m.jobs.c.company_id).where(m.jobs.c.id == self.job_id)).scalar_one()
            c.execute(update(m.jobs).where(m.jobs.c.id == self.job_id).values(title='Role api_key=SYNTHETIC_PRIVATE'))
            c.execute(update(m.companies).where(m.companies.c.id == company_id).values(name='Bearer SYNTHETIC_PRIVATE'))
            record_job_merged(c, 'fixture', self.job_id, 'private-posting')
        merged = next(e for e in self.history() if e['type'] == 'JOB_MERGED')
        self.assertEqual(merged['subject'], 'Details withheld')
        self.assertEqual(merged['company'], '')
        with self.store.engine.connect() as c:
            payload = c.execute(select(m.audit.c.payload).where(m.audit.c.id == merged['id'])).scalar_one()
            self.assertEqual(payload['subject_snapshot'], {'subject': 'Details withheld', 'company': ''})
            self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(payload))
            self.assertIn('SYNTHETIC_PRIVATE', c.execute(select(m.jobs.c.title).where(m.jobs.c.id == self.job_id)).scalar_one())

    def test_malformed_and_private_snapshots_fail_closed_without_reflecting_payload(self):
        for snapshot in (None, 'private', [], {'subject': []},
                         {'subject': 'x' * 501, 'company': 'api_key=SYNTHETIC_PRIVATE'},
                         {'subject': 'Role', 'company': {}, 'notes': 'SYNTHETIC_PRIVATE'},
                         {'subject': 'password=SYNTHETIC_PRIVATE', 'company': 'https://example.invalid/private'}):
            with self.subTest(snapshot=snapshot), self.store.engine.begin() as c:
                c.execute(delete(m.audit).where(m.audit.c.id == 'invalid-snapshot'))
                c.execute(m.audit.insert().values(id='invalid-snapshot', entity_id='fixture', action='JOB_DISCOVERED',
                          payload={'job_id': self.job_id, 'subject_snapshot': snapshot,
                                   'private_note': 'SYNTHETIC_PRIVATE'}, timestamp='2026-10-14T07:00:00Z'))
            events = self.history()
            event = next(e for e in events if e['id'] == 'invalid-snapshot')
            self.assertEqual(event['subject'], 'Details withheld')
            self.assertEqual(event['company'], '')
            self.assertEqual(set(event), {'id', 'type', 'summary', 'subject', 'company', 'timestamp'})
            self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(events))


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'Requires isolated disposable PostgreSQL')
class AuditSubjectPrivacyPostgresTests(AuditSubjectPrivacyTests):
    _new_store = postgres_fixture.LeverFormReviewPostgresFlowTests._new_store
    _drop_schema = postgres_fixture.LeverFormReviewPostgresFlowTests._drop_schema
