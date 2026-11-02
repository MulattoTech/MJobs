"""Profile ORM reads preserve security, immutable snapshots and Core ownership."""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from packages.persistence import models as m
from packages.persistence.profile_records import (
    CareerFactRecord,
    CareerProfileRecord,
    ProfileVersionRecord,
)
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class ProfileOrmReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-profile-orm-")
        self.addCleanup(self.directory.cleanup)
        self.url = f"sqlite:///{(Path(self.directory.name) / 'profile.db').as_posix()}"
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.save_profile(deepcopy(PROFILE))

    def test_real_mapped_read_and_historical_snapshot_survive_reopen(self):
        mapped = set()
        def observe(state):
            mapped.update(mapper.class_ for mapper in state.all_mappers)
        event.listen(Session, "do_orm_execute", observe)
        try:
            first = self.store.profile("fixture")
        finally:
            event.remove(Session, "do_orm_execute", observe)
        self.assertEqual(mapped, {CareerProfileRecord, ProfileVersionRecord, CareerFactRecord})
        changed = deepcopy(PROFILE)
        changed["name"] = "New reviewed name"
        changed["answers"][0]["text"] = "New answer"
        self.store.save_profile(changed)
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(reopened.profile("fixture", first["version"]), first)
        current = reopened.profile("fixture")
        self.assertEqual(current["name"], changed["name"])
        self.assertNotEqual(current["version"], first["version"])
        self.assertFalse(current["answers"][0]["user_approved"])

    def test_injection_shaped_identity_and_text_are_bound_data(self):
        payload = "x' OR 1=1; DROP TABLE career_profiles; --"
        profile = deepcopy(PROFILE)
        profile["id"] = payload
        profile["facts"][0]["text"] = payload
        self.store.save_profile(profile)
        observed = []
        def capture(_connection, _cursor, statement, parameters, *_args):
            observed.append((statement, parameters))
        event.listen(self.store.engine, "before_cursor_execute", capture)
        try:
            actual = self.store.profile(payload)
        finally:
            event.remove(self.store.engine, "before_cursor_execute", capture)
        self.assertEqual(actual["id"], payload)
        self.assertEqual(next(f for f in actual["facts"] if f["fact_id"] == "python")["text"], payload)
        self.assertTrue(any(payload in parameters for _, parameters in observed))
        self.assertTrue(all(payload not in statement for statement, _ in observed))
        with self.assertRaises(NoResultFound):
            self.store.profile("missing' OR 1=1 --")
        self.assertEqual(self.store.count("career_profiles"), 2)
        self.assertEqual(self.store.profile("fixture")["name"], PROFILE["name"])

    def test_returned_values_are_detached_from_persisted_json(self):
        original = self.store.profile("fixture")
        changed = self.store.profile("fixture")
        changed["answers"][0]["text"] = "unapproved caller mutation"
        changed["preferences"]["avoid_terms"] = ["caller-only"]
        changed["facts"][0]["text"] = "caller-only"
        self.assertEqual(self.store.profile("fixture"), original)

    def test_session_close_preserves_idle_active_and_savepoint_ownership(self):
        for kind in ("idle", "active", "savepoint"):
            with self.subTest(kind=kind), self.store.engine.connect() as connection:
                if kind != "idle":
                    connection.begin()
                    # SQLite needs a physical outer transaction before SAVEPOINT.
                    connection.exec_driver_sql("BEGIN")
                nested = connection.begin_nested() if kind == "savepoint" else None
                prior_transaction = connection.get_transaction()
                self.store.profile("fixture", connection=connection)
                transaction = connection.get_transaction()
                if prior_transaction is not None:
                    self.assertIs(transaction, prior_transaction)
                self.assertTrue(transaction.is_active)
                self.assertEqual(connection.get_nested_transaction(), nested)
                self.store._audit(connection, "fixture", "ORM_CALLER_SENTINEL", {"scope": kind})
                for identifier in ("fixture", "missing"):
                    try:
                        self.store.profile(identifier, connection=connection)
                    except NoResultFound:
                        self.assertEqual(identifier, "missing")
                    self.assertIs(connection.get_transaction(), transaction)
                    self.assertTrue(transaction.is_active)
                    if nested:
                        self.assertTrue(nested.is_active)
                    self.assertIsNotNone(connection.execute(select(m.audit.c.id).where(
                        m.audit.c.action == "ORM_CALLER_SENTINEL")).first())
                connection.rollback()
            with self.store.engine.connect() as fresh:
                self.assertIsNone(fresh.execute(select(m.audit.c.id).where(
                    m.audit.c.action == "ORM_CALLER_SENTINEL")).first())

    def test_fresh_session_does_not_reuse_prior_profile_identity(self):
        first = self.store.profile("fixture")
        changed = deepcopy(PROFILE)
        changed["name"] = "Second snapshot"
        self.store.save_profile(changed)
        self.assertEqual(self.store.profile("fixture")["name"], "Second snapshot")
        self.assertEqual(self.store.profile("fixture", first["version"]), first)
