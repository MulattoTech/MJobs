"""Execute the same profile-scope API contract on isolated PostgreSQL schemas."""
import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text

from packages.persistence import models as m
from tests.integration import test_operation_profile_scope as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url


POSTGRES_URL = os.environ.get("MJOBS_AI_EVALUATION_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL")
class OperationProfileScopePostgreSQLTests(contract.OperationProfileScopeTests):
    def _database_url(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        schema = "mjobs_operation_scope_" + uuid4().hex
        admin = create_engine(url)
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))

        def drop_schema():
            with admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))

        self.addCleanup(drop_schema)
        return url.update_query_dict({
            "options": "-csearch_path=" + schema + " -clock_timeout=5000 -cstatement_timeout=30000",
        }).render_as_string(hide_password=False)

    def _initialize_store(self):
        from packages.persistence import resume_workspace  # noqa: F401

        m.metadata.create_all(self.store.engine)
