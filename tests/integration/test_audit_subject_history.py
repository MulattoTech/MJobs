"""Activity preserves transaction-time subjects without borrowing current labels."""
import os
import unittest

from sqlalchemy import delete, select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.audit_history import (
    record_application_preparation_event, record_company_discovered,
    record_company_source_enrolled, record_job_discovered, record_job_merged,
    record_match_scored,
)
from services.discovery.matching import recalculate
from tests.integration import test_document_generation_audit as fixture
from tests.integration import test_lever_form_review_flow as postgres_fixture


class AuditSubjectHistoryTests(unittest.TestCase):
    _new_store = fixture.DocumentGenerationAuditTests._new_store
    setUp = fixture.DocumentGenerationAuditTests.setUp
    _client = fixture.DocumentGenerationAuditTests._client
    tearDown = fixture.DocumentGenerationAuditTests.tearDown
    history = fixture.DocumentGenerationAuditTests.history

    def job_events(self):
        return [e for e in self.history() if e['type'] in {'JOB_DISCOVERED', 'MATCH_SCORED'}]

    def test_discovery_subjects_survive_rename_replay_and_database_reopen(self):
        before = self.job_events()
        self.assertTrue(before)
        with self.store.engine.begin() as c:
            c.execute(update(m.jobs).values(title='Renamed current job'))
            c.execute(update(m.companies).values(name='Renamed current employer'))
        recalculate(self.store, 'fixture')
        self.assertEqual(self.job_events(), before)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self.job_events(), before)
        self.assertFalse(any(e['type'] in {'JOB_DISCOVERED', 'MATCH_SCORED'}
                             for e in self.history('other')))

    def test_deleted_subjects_keep_all_profile_event_types_and_replays_keep_first_snapshot(self):
        # Standalone canonical rows have no dependent product records. This
        # deletion is valid under both SQLite and real PostgreSQL constraints.
        with self.store.engine.begin() as c:
            c.execute(m.companies.insert().values(id='historical-company', name='Original Employer', domain='example.invalid'))
            c.execute(m.jobs.insert().values(id='historical-job', company_id='historical-company',
                      requisition='history', title='Original Role', description='Fixture', skills=[],
                      location='Remote', remote_type='REMOTE'))
            record_job_discovered(c, 'fixture', 'historical-job')
            record_match_scored(c, 'fixture', 'historical-job', 'version-1')
            record_job_merged(c, 'fixture', 'historical-job', 'posting-1')
            record_company_discovered(c, 'fixture', 'historical-company')
            record_company_source_enrolled(c, 'fixture', 'historical-company', 'source-1')
            for action in ('APPLICATION_QUEUED', 'APPLICATION_STARTED', 'APPLICATION_FAILED'):
                record_application_preparation_event(c, action, 'fixture', 'historical-job', 'operation-1', 1)
        before = [e for e in self.history() if e['subject'] in {'Original Role', 'Original Employer'}]
        self.assertEqual(len(before), 8)
        with self.store.engine.begin() as c:
            c.execute(update(m.jobs).where(m.jobs.c.id == 'historical-job').values(title='Later Role'))
            record_job_discovered(c, 'fixture', 'historical-job')
            record_company_discovered(c, 'fixture', 'historical-company')
            c.execute(delete(m.jobs).where(m.jobs.c.id == 'historical-job'))
            c.execute(delete(m.companies).where(m.companies.c.id == 'historical-company'))
        after = [e for e in self.history() if e['id'] in {e['id'] for e in before}]
        self.assertEqual(after, before)

    def test_legacy_subject_is_explicitly_unknown_even_when_current_labels_exist(self):
        with self.store.engine.begin() as c:
            c.execute(m.audit.insert().values(id='legacy-subject', entity_id='fixture', action='JOB_DISCOVERED',
                      payload={'job_id': self.job_id}, timestamp='2026-10-14T07:00:00Z'))
        event = next(e for e in self.history() if e['id'] == 'legacy-subject')
        self.assertEqual(event['subject'], 'Historical details not recorded')
        self.assertEqual(event['company'], '')
        self.assertEqual(event['type'], 'JOB_DISCOVERED')

    def test_rollback_does_not_leave_subject_history_and_retry_appends_once(self):
        before = self.history()
        with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
            with self.store.engine.begin() as c:
                record_job_merged(c, 'fixture', self.job_id, 'rollback-posting')
                raise RuntimeError('synthetic rollback')
        self.assertEqual(self.history(), before)
        for _ in range(2):
            with self.store.engine.begin() as c:
                record_job_merged(c, 'fixture', self.job_id, 'rollback-posting')
        added = [e for e in self.history() if e['id'] not in {e['id'] for e in before}]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['type'], 'JOB_MERGED')


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'Requires isolated disposable PostgreSQL')
class AuditSubjectHistoryPostgresTests(AuditSubjectHistoryTests):
    _new_store = postgres_fixture.LeverFormReviewPostgresFlowTests._new_store
    _drop_schema = postgres_fixture.LeverFormReviewPostgresFlowTests._drop_schema
