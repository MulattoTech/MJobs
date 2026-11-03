"""AI resume receipt and historical fact contracts on disposable PostgreSQL."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_ai_resume_fact_trace as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url

POSTGRES_URL = os.environ.get("MJOBS_AI_EVALUATION_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL")
class AIResumePostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        self.schema = "mjobs_ai_resume_" + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.cleanup_schema)
        database_url = url.update_query_dict({
            "options": "-csearch_path=" + self.schema
                       + " -clock_timeout=5000 -cstatement_timeout=30000",
        }).render_as_string(hide_password=False)

        class TestStore(Store):
            def __init__(self, _ignored):
                super().__init__(database_url)

            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)

        store_patch = patch.object(contract, "Store", TestStore)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.case = contract.AIResumeTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.case.url = database_url

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_changed_ai_wording_and_sources_survive_reopen(self):
        self.case.test_changed_ai_wording_and_original_sources_survive_reopen()

    def test_concurrent_request_calls_provider_once(self):
        self.case.test_concurrent_same_request_has_only_one_provider_call()

    def test_acceptance_is_idempotent(self):
        self.case.test_exact_request_and_acceptance_are_idempotent()

    def test_stale_profile_and_job_refuse_preparation(self):
        self.case.test_profile_approval_and_job_changes_refuse_stale_proposals()
