"""Actual row-lock and two-worker proof on an isolated disposable PostgreSQL schema."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_route_attempt_concurrency as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url

POSTGRES_URL = os.environ.get('MJOBS_TEST_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Requires disposable loopback MJOBS_TEST_POSTGRES_URL')
class RouteAttemptPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        self.schema = 'mjobs_route_attempt_' + uuid4().hex
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
        store_patch = patch.object(contract, 'Store', TestStore)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.case = contract.RouteAttemptConcurrencyTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.case.url = database_url

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_parallel_reservation_and_restart(self):
        self.case.test_independent_connections_race_and_restart_cannot_steal_route()

    def test_two_real_workers_submit_once_and_reviewed_retry(self):
        self.case.test_two_real_workers_only_one_submits_then_explicit_retry_is_safe()

    def test_independent_routes_and_unknown_legacy_owner(self):
        self.case.test_other_routes_remain_available_and_unknown_legacy_owner_fails_closed()

    def test_completed_replay_while_other_application_owns_route(self):
        self.case.test_completed_replay_does_not_reacquire_route_held_by_another_application()

    def test_receipt_recovery_and_ambiguous_owner(self):
        self.case.test_receipt_recovery_releases_only_proven_fenced_confirmed_owner()
