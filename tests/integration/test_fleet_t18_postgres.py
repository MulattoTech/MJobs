"""Profile creation fences on an explicit disposable loopback PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.pool import NullPool

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_fleet_t18_fencing as fencing_contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url


POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "Requires disposable loopback MJOBS_TEST_POSTGRES_URL")
class ProfileWritePostgresTests(fencing_contract.ProfileWriteFencingTests):
    def setUp(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        schema = "mjobs_t18_" + uuid4().hex
        self.application_name = schema
        admin = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 5})
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))

        def cleanup_schema():
            try:
                with admin.begin() as connection:
                    connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
            finally:
                admin.dispose()

        self.addCleanup(cleanup_schema)
        self.url = url.update_query_dict({"options": "-csearch_path=" + schema
            + " -clock_timeout=5000 -cstatement_timeout=30000 -capplication_name=" + schema
        }).render_as_string(hide_password=False)
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        from packages.persistence import resume_workspace  # noqa: F401
        m.metadata.create_all(self.store.engine)
        self.store.bind_workspace_mode("FIXTURE")
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t18-pg-")
        self.addCleanup(self.directory.cleanup)

    def test_fresh_public_profile_create_and_returning_conflict_readback(self):
        profile = self.competing_profile("Synthetic PostgreSQL creator", "pg-fact")
        profile["id"] = "t18-api-" + uuid4().hex
        app = create_app(self.store, "http://127.0.0.1:8090", Path(self.directory.name),
                         "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        with TestClient(app) as client:
            created = client.put("/profiles", json=profile)
            self.assertEqual(created.status_code, 200, created.text)
            readback = client.get("/profiles/" + profile["id"])
            self.assertEqual(readback.status_code, 200, readback.text)
            self.assertEqual(readback.json()["version"], created.json()["version"])
            self.assertEqual(client.put("/profiles", json=profile).status_code, 409)
        reopened = Store(self.url)
        try:
            self.assertEqual(reopened.profile(profile["id"])["version"], created.json()["version"])
        finally:
            reopened.engine.dispose()

        # Observe the actual driver metadata without making correctness depend
        # on its rowcount value; explicit RETURNING must distinguish conflicts.
        probe = {"id": "t18-probe-" + uuid4().hex, "name": "Synthetic probe",
                 "email": "probe@example.invalid", "version": "probe"}
        with self.store.engine.begin() as connection:
            statement = insert(m.profiles).values(**probe).on_conflict_do_nothing(
                index_elements=[m.profiles.c.id]).returning(m.profiles.c.id)
            result = connection.execute(statement)
            rowcount = result.rowcount
            self.assertEqual(result.scalar_one_or_none(), probe["id"])
            self.assertIsNone(connection.execute(statement).scalar_one_or_none())
        print("PostgreSQL INSERT RETURNING rowcount:", rowcount)

    def test_waiting_creator_succeeds_after_competing_transaction_rolls_back(self):
        profile = self.competing_profile("Synthetic rollback survivor", "rollback-fact")
        profile["id"] = "t18-rollback-" + uuid4().hex
        contender = Store(self.url)
        self.addCleanup(contender.engine.dispose)
        with self.store.engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(m.profiles.insert().values(
                id=profile["id"], name="Uncommitted placeholder",
                email="uncommitted@example.invalid", version="uncommitted"))
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(contender.save_profile, profile)
                try:
                    deadline = time.monotonic() + 5
                    blocked = False
                    while time.monotonic() < deadline:
                        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                        blocked = bool(connection.execute(text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND application_name = :name "
                            "AND wait_event_type = 'Lock' AND state = 'active'"
                        ), {"name": self.application_name}).scalar_one())
                        if blocked:
                            break
                        time.sleep(0.02)
                    self.assertTrue(blocked, "contending insert never waited on the uncommitted creator")
                    self.assertFalse(future.done())
                finally:
                    transaction.rollback()
                version = future.result(timeout=10)
        reopened = Store(self.url)
        try:
            self.assertEqual(reopened.profile(profile["id"])["version"], version)
            with reopened.engine.connect() as connection:
                versions = connection.execute(select(m.profile_versions.c.version).where(
                    m.profile_versions.c.profile_id == profile["id"])).scalars().all()
                audits = connection.execute(select(m.audit.c.id).where(
                    m.audit.c.entity_id == profile["id"], m.audit.c.action == "PROFILE_SAVED")).all()
            self.assertEqual(versions, [version])
            self.assertEqual(len(audits), 1)
        finally:
            reopened.engine.dispose()
