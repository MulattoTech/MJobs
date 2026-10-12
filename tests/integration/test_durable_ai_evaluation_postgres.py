"""Opt-in real PostgreSQL transaction and recovery proof in an isolated schema."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_durable_ai_evaluation as contract

POSTGRES_URL = os.environ.get('MJOBS_AI_EVALUATION_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Set MJOBS_AI_EVALUATION_POSTGRES_URL to a disposable loopback mjobs_test_* database')
class DurableAIEvaluationPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith('postgresql') or url.host not in ('localhost', '127.0.0.1', '::1')
                or not url.database or not url.database.startswith('mjobs_test_')):
            raise RuntimeError('A disposable loopback mjobs_test_* PostgreSQL database is required')
        self.schema = 'mjobs_ai_' + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        scoped = url.update_query_dict({'options': '-csearch_path=' + self.schema + ' -clock_timeout=5000 -cstatement_timeout=30000'})
        database_url = scoped.render_as_string(hide_password=False)
        class PostgreSQLTestStore(Store):
            def __init__(self, _ignored):
                super().__init__(database_url)
            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)
        self.case = contract.DurableAIEvaluationTests()
        store_patch = patch.object(contract, 'Store', PostgreSQLTestStore)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.case.setUp()
        self.case.url = database_url
        self.addCleanup(self.case.doCleanups)

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_admission_replay_and_history_on_postgresql(self):
        self.case.test_admission_reload_replay_worker_and_history()

    def test_atomic_publication_and_staged_response_recovery_on_postgresql(self):
        self.case.test_result_publication_failure_rolls_back_completion_and_reuses_staged_response()

    def test_uncertain_call_blocks_replay_on_postgresql(self):
        self.case.test_expired_started_provider_call_is_never_replayed()

    def test_cancel_during_call_on_postgresql(self):
        self.case.test_cancel_during_provider_call_never_publishes_partial_history()

    def test_stale_worker_fence_on_postgresql(self):
        self.case.test_stale_worker_cannot_publish_after_recovery_blocks_uncertain_call()

    def test_profile_edit_fence_on_postgresql(self):
        self.case.test_profile_edit_during_provider_call_fences_whole_batch()
