"""Run the same reciprocal admission contract in a disposable PostgreSQL schema."""
import os
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_application_run_requests as fixtures
from tests.integration import test_fleet_t09_reciprocal_fence as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url


@pytest.fixture
def postgres_case():
    value = os.environ.get("MJOBS_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("Requires disposable loopback MJOBS_TEST_POSTGRES_URL")
    url = _disposable_postgres_url(value)
    schema = "mjobs_fleet_t09_" + uuid4().hex
    admin = create_engine(url)
    created = False
    try:
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        created = True
        database_url = url.update_query_dict({"options": "-csearch_path=" + schema
            + " -clock_timeout=5000 -cstatement_timeout=30000"}).render_as_string(hide_password=False)

        class TestStore(Store):
            def __init__(self, ignored):
                super().__init__(database_url)

            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)

        with patch.object(fixtures, "Store", TestStore), patch.object(contract, "Store", TestStore):
            case = fixtures.ApplicationRunRequestTests()
            try:
                case.setUp()
                yield case
            finally:
                if hasattr(case, "store"):
                    case.store.engine.dispose()
                if hasattr(case, "temp"):
                    case.temp.cleanup()
    finally:
        try:
            if created:
                with admin.begin() as connection:
                    connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        finally:
            admin.dispose()


def test_running_physical_attempt_blocks_manual_after_postgres_reopen(postgres_case):
    contract.test_running_physical_attempt_blocks_manual_admission_after_reopen(postgres_case)


@pytest.mark.parametrize("ordering", ["simultaneous", "physical_locked", "manual_locked"])
def test_postgres_concurrent_admission_has_one_owner(postgres_case, ordering):
    contract.test_concurrent_physical_and_manual_admission_has_exactly_one_owner(postgres_case, ordering)


def test_postgres_different_application_remains_available(postgres_case):
    contract.test_different_application_manual_admission_remains_available_at_capacity_two(postgres_case)
