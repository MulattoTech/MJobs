"""Grounded candidate admission and frozen history on disposable PostgreSQL."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_durable_ai_evaluation as durable
from tests.integration import test_grounded_ai_candidates as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url

POSTGRES_URL = os.environ.get('MJOBS_AI_EVALUATION_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL')
class GroundedAICandidatePostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        self.schema = 'mjobs_grounded_ai_' + uuid4().hex
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
        self.case = contract.GroundedAICandidateTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.case.url = database_url

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_current_evidence_outranks_cache(self):
        self.case.test_current_approved_evidence_outranks_stale_cached_score()

    def test_unknown_coverage_is_not_admitted(self):
        self.case.test_unknown_coverage_is_disclosed_and_never_admitted_as_zero()

    def test_disabled_mode_and_missing_cache(self):
        self.case.test_missing_cached_match_does_not_hide_current_evidence_and_disabled_mode_is_local()

    def test_real_worker_frozen_history_and_privacy(self):
        self.case.test_actual_worker_persists_frozen_explanation_without_sending_profile_evidence()

    def test_stale_preview_rejected(self):
        self.case.test_changed_evidence_requires_new_preview_before_admission()

    def test_unapproved_evidence_and_profile_isolation(self):
        self.case.test_unapproved_facts_do_not_award_evidence_credit()
