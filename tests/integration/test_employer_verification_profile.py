"""Actual local profile API/Store activation; no bootstrap or external transport."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import os
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update

from apps.api.main import create_app
from packages.domain.employer_verification import (
    GREENHOUSE_RECIPROCAL, disabled_preference, verification_policy_snapshot,
)
from packages.persistence import models as m
from packages.persistence.profile_restore import apply_profile_restore
from packages.persistence.store import ProfileVersionConflict, Store, identity
from services.data_export import export_profile_backup
from services.profile_restore import build_restore_plan


class EmployerVerificationProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="employer-verification-profile-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = self._new_store()
        self.store.bind_workspace_mode("PERSONAL")
        self.profile_id = "verification-profile"
        self.store.save_profile({
            "id": self.profile_id, "name": "Synthetic Person",
            "email": "person@example.test", "facts": [], "answers": [],
        })
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        ))
        self.addCleanup(self.client.close)
        self.initial_work = self.work_counts()

    def _new_store(self):
        store = Store(f"sqlite:///{(self.root / 'profile.db').as_posix()}")
        self.addCleanup(store.engine.dispose)
        store.migrate()
        self.assertEqual(store.engine.dialect.name, "sqlite")
        return store

    def work_counts(self):
        with self.store.engine.connect() as connection:
            return {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                    for table in (m.operations, m.job_sources, m.company_sources)}

    def read(self, profile_id=None):
        response = self.client.get("/profiles/" + (profile_id or self.profile_id))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def payload(self, profile=None):
        profile = profile or self.read()
        return {**{key: deepcopy(profile[key]) for key in (
            "id", "name", "email", "contact", "preferences", "facts", "answers")},
            "expected_version": profile["version"]}

    def grant(self, identifier=None):
        return {"version": 1, "mode": GREENHOUSE_RECIPROCAL, "activation_id": identifier}

    def save(self, payload):
        response = self.client.put("/profiles", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        result = self.read(payload["id"])
        self.assertEqual(response.json()["version"], result["version"])
        return result

    def activate(self):
        payload = self.payload()
        payload["preferences"]["company_discovery"]["employer_verification"] = self.grant()
        return self.save(payload)

    def value(self, profile):
        return profile["preferences"]["company_discovery"]["employer_verification"]

    def assert_no_work(self):
        self.assertEqual(self.work_counts(), self.initial_work)

    def test_default_and_readback_do_not_activate_or_dispatch(self):
        profile = self.read()
        self.assertEqual(self.value(profile), disabled_preference())
        self.assertIsNone(verification_policy_snapshot(profile))
        self.assertEqual(self.read(), profile)
        self.assert_no_work()

    def test_existing_snapshot_without_new_preference_is_not_rewritten_on_read(self):
        legacy = self.payload()
        legacy.pop("expected_version")
        legacy["preferences"]["company_discovery"].pop("employer_verification")
        version = identity(legacy)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.profiles).where(m.profiles.c.id == self.profile_id).values(version=version))
            connection.execute(m.profile_versions.insert().values(
                id=identity(self.profile_id, version), profile_id=self.profile_id, version=version,
                contact={key: deepcopy(legacy[key]) for key in ("name", "email", "contact", "preferences")},
                answers=[],
            ))
        with self.store.engine.connect() as connection:
            before = [dict(row) for row in connection.execute(select(m.profile_versions)).mappings()]
        first = self.read()
        self.assertNotIn("employer_verification", first["preferences"]["company_discovery"])
        self.assertIsNone(verification_policy_snapshot(first))
        self.assertEqual(self.read(), first)
        with self.store.engine.connect() as connection:
            after = [dict(row) for row in connection.execute(select(m.profile_versions)).mappings()]
        self.assertEqual(after, before)
        self.assertEqual(first["version"], version)

    def test_explicit_save_mints_bound_authorization_and_does_not_start_work(self):
        before = self.read()
        active = self.activate()
        self.assertNotEqual(active["version"], before["version"])
        self.assertRegex(self.value(active)["activation_id"], r"^[0-9a-f]{32}$")
        policy = verification_policy_snapshot(active)
        self.assertEqual(policy["profile_id"], active["id"])
        self.assertEqual(policy["profile_version"], active["version"])
        self.assertEqual(policy["activation_id"], self.value(active)["activation_id"])
        self.assertEqual((policy["rate_limit"], policy["request_timeout"], policy["concurrency"]), (30, 10, 1))
        self.assert_no_work()

    def test_active_echo_or_null_preserves_id_across_ordinary_profile_edits(self):
        current = self.activate()
        identifier = self.value(current)["activation_id"]
        for index, echoed in enumerate((identifier, None)):
            payload = self.payload(current)
            payload["name"] = f"Synthetic Person {index}"
            payload["preferences"]["company_discovery"]["employer_verification"] = self.grant(echoed)
            current = self.save(payload)
            self.assertEqual(self.value(current)["activation_id"], identifier)
        self.assert_no_work()

    def test_disable_and_reenable_rotate_and_reject_old_or_foreign_id(self):
        active = self.activate()
        old_id = self.value(active)["activation_id"]
        payload = self.payload(active)
        payload["preferences"]["company_discovery"]["employer_verification"] = disabled_preference()
        disabled = self.save(payload)
        self.assertEqual(self.value(disabled), disabled_preference())
        replay = self.payload(disabled)
        replay["preferences"]["company_discovery"]["employer_verification"] = self.grant(old_id)
        response = self.client.put("/profiles", json=replay)
        self.assertIn(response.status_code, (400, 422))
        self.assertIn("EMPLOYER_VERIFICATION_ACTIVATION_ID_CHANGED", response.text)
        self.assertEqual(self.read(), disabled)
        new_active = self.activate()
        self.assertNotEqual(self.value(new_active)["activation_id"], old_id)
        foreign = self.payload(new_active)
        foreign["preferences"]["company_discovery"]["employer_verification"] = self.grant("f" * 32)
        response = self.client.put("/profiles", json=foreign)
        self.assertIn(response.status_code, (400, 422))
        self.assertEqual(self.read(), new_active)

    def test_omitted_preferences_preserve_active_but_submitted_omission_revokes(self):
        active = self.activate()
        payload = self.payload(active)
        payload.pop("preferences")
        payload["name"] = "Synthetic Updated"
        preserved = self.save(payload)
        self.assertEqual(self.value(preserved), self.value(active))
        payload = self.payload(preserved)
        payload["preferences"]["company_discovery"].pop("employer_verification")
        revoked = self.save(payload)
        self.assertEqual(self.value(revoked), disabled_preference())
        self.assertIsNone(verification_policy_snapshot(revoked))

    def test_discovery_disable_clears_echoed_grant_and_reenable_does_not_activate(self):
        active = self.activate()
        payload = self.payload(active)
        payload["preferences"]["company_discovery"]["company_discovery_enabled"] = False
        revoked = self.save(payload)
        self.assertEqual(self.value(revoked), disabled_preference())
        payload = self.payload(revoked)
        payload["preferences"]["company_discovery"]["company_discovery_enabled"] = True
        reenabled = self.save(payload)
        self.assertEqual(self.value(reenabled), disabled_preference())
        self.assert_no_work()

    def test_stale_or_missing_expected_version_cannot_commit_activation(self):
        stale = self.payload()
        current = self.payload()
        current["name"] = "A newer saved profile"
        latest = self.save(current)
        stale["preferences"]["company_discovery"]["employer_verification"] = self.grant()
        for payload in (stale, {key: value for key, value in stale.items() if key != "expected_version"}):
            with self.subTest(expected_version=payload.get("expected_version")):
                response = self.client.put("/profiles", json=payload)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.read(), latest)
        self.assert_no_work()

    def test_activation_id_cannot_be_transplanted_to_another_profile(self):
        active = self.activate()
        payload = self.payload(active)
        payload["id"] = "another-profile"
        payload.pop("expected_version")
        response = self.client.put("/profiles", json=payload)
        self.assertIn(response.status_code, (400, 422))
        self.assertIn("EMPLOYER_VERIFICATION_ACTIVATION_ID_CHANGED", response.text)
        with self.store.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == "another-profile")).scalar_one_or_none())
        payload["preferences"]["company_discovery"]["employer_verification"] = self.grant()
        created = self.save(payload)
        self.assertNotEqual(self.value(created)["activation_id"], self.value(active)["activation_id"])

    def test_overlapping_profile_saves_commit_only_one_activation(self):
        initial = self.read()
        barrier = threading.Barrier(2, timeout=5)
        local = threading.local()
        backend_pids = set()
        backend_pids_lock = threading.Lock()
        def before_execute(connection, cursor, statement, parameters, context, executemany):
            if (statement.startswith("UPDATE " + m.profiles.name + " SET version=")
                    and not getattr(local, "entered", False)):
                local.entered = True
                if connection.dialect.name == "postgresql":
                    backend_pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                    with backend_pids_lock:
                        backend_pids.add(backend_pid)
                barrier.wait()
        def save():
            payload = self.payload(initial)
            expected = payload.pop("expected_version")
            payload["preferences"]["company_discovery"]["employer_verification"] = self.grant()
            try:
                version = self.store.save_profile(payload, expected_version=expected, require_expected_version=True)
                return ("COMMITTED", version)
            except ProfileVersionConflict:
                return ("CONFLICT", None)
        event.listen(self.store.engine, "before_cursor_execute", before_execute)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(save), pool.submit(save)
                results = [first.result(timeout=10), second.result(timeout=10)]
        finally:
            event.remove(self.store.engine, "before_cursor_execute", before_execute)
        self.assertEqual(sorted(result[0] for result in results), ["COMMITTED", "CONFLICT"])
        if self.store.engine.dialect.name == "postgresql":
            self.assertEqual(len(backend_pids), 2, "Concurrent saves must use two real PostgreSQL sessions")
        saved = self.read()
        self.assertEqual(saved["version"], next(value for state, value in results if state == "COMMITTED"))
        self.assertRegex(self.value(saved)["activation_id"], r"^[0-9a-f]{32}$")
        self.assert_no_work()

    def test_actual_restore_clears_every_imported_activation_before_reenable(self):
        active = self.activate()
        old_id = self.value(active)["activation_id"]
        payload = self.payload(active)
        payload["name"] = "Synthetic Active Revision"
        active = self.save(payload)
        backup = export_profile_backup(self.store, self.profile_id)
        plan = build_restore_plan(backup, "restored-verification-profile")
        original = deepcopy(plan)
        receipt = apply_profile_restore(self.store, plan, self.root / "restored-files", "verification-restore")
        self.assertEqual(receipt["profile_id"], "restored-verification-profile")
        self.assertEqual(plan, original)
        restored = self.read(receipt["profile_id"])
        self.assertEqual(self.value(restored), disabled_preference())
        self.assertFalse(restored["preferences"]["company_discovery"]["company_discovery_enabled"])
        with self.store.engine.connect() as connection:
            snapshots = connection.execute(select(m.profile_versions.c.contact).where(
                m.profile_versions.c.profile_id == restored["id"])).scalars().all()
        self.assertGreaterEqual(len(snapshots), 2)
        for snapshot in snapshots:
            self.assertEqual(snapshot["preferences"]["company_discovery"]["employer_verification"],
                             disabled_preference())
        request = self.payload(restored)
        request["preferences"]["company_discovery"]["company_discovery_enabled"] = True
        request["preferences"]["company_discovery"]["employer_verification"] = self.grant(old_id)
        response = self.client.put("/profiles", json=request)
        self.assertIn(response.status_code, (400, 422))
        self.assertEqual(self.read(restored["id"]), restored)
        request["preferences"]["company_discovery"]["employer_verification"] = self.grant()
        enabled = self.save(request)
        self.assertNotEqual(self.value(enabled)["activation_id"], old_id)
        self.assert_no_work()


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "disposable PostgreSQL not configured")
class EmployerVerificationProfilePostgresTests(EmployerVerificationProfileTests):
    """Run every activation/API/CAS/restore case in its own real PostgreSQL schema."""

    from tests.integration.test_lever_form_review_flow import (
        LeverFormReviewPostgresFlowTests as _Fixture,
    )
    _drop_schema = _Fixture._drop_schema

    def _new_store(self):
        store = self._Fixture._new_store(self)
        self.assertEqual(store.engine.dialect.name, "postgresql")
        return store
