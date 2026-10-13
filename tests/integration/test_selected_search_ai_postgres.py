"""Selected-search snapshot/history and filtering on disposable PostgreSQL."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_durable_ai_evaluation as durable
from tests.integration import test_selected_search_ai as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url

POSTGRES_URL = os.environ.get('MJOBS_AI_EVALUATION_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL')
class SelectedSearchAIPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        self.schema = 'mjobs_scoped_ai_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.cleanup_schema)
        database_url = url.update_query_dict({'options': '-csearch_path=' + self.schema +
            ' -clock_timeout=5000 -cstatement_timeout=30000'}).render_as_string(hide_password=False)
        class TestStore(Store):
            def __init__(self, ignored):
                super().__init__(database_url)
            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)
        store_patch = patch.object(durable, 'Store', TestStore)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.case = contract.SelectedSearchAITests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.case.url = database_url

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_opposite_scopes_and_durable_history(self):
        self.case.test_opposite_scopes_freeze_scores_history_and_never_rewrite_matches()

    def test_saved_filters_and_dismissal(self):
        self.case.test_saved_filters_use_recomputed_score_and_dismissal_before_admission()

    def test_admission_identity_and_owner_scope(self):
        self.case.test_same_request_cannot_be_reused_for_another_scope()

    def test_edit_after_admission_and_worker_replay(self):
        self.case.test_admitted_search_edit_keeps_original_snapshot_and_replay_does_not_call_twice()
