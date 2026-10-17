"""Durable, owner-scoped controls for local controlled application rehearsals."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.applications.rehearsal_store import (
    RehearsalStoreError,
    get_controlled_controls,
    record_controlled_admission,
    require_controlled_admission,
    require_controlled_execution,
    set_controlled_controls,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ControlledRehearsalControlsFleetT16BTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t16-controls-")
        root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(root / 'controls.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, job["id"], "fixture", root)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _set(self, request_id="controls-1", **values):
        return set_controlled_controls(
            self.store, "fixture", self.application_id,
            request_id=request_id, **values,
        )

    def _insert_operation(self, operation_id):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id, kind="APPLICATION_DRY_RUN",
                entity_id=self.application_id, params={
                    "profile_id": "fixture", "application_id": self.application_id,
                }, state="QUEUED", error=None,
                updated_at="2026-10-17T00:00:00+00:00", attempt_count=0,
                cancel_requested=False,
            ))

    def test_defaults_are_conservative_and_submission_stays_disabled(self):
        controls = get_controlled_controls(self.store, "fixture", self.application_id)

        self.assertEqual(controls["profile_id"], "fixture")
        self.assertEqual(controls["application_id"], self.application_id)
        self.assertIsNone(controls["request_id"])
        self.assertEqual(controls["paused"], False)
        self.assertEqual(controls["daily_limit"], 10)
        self.assertIs(controls["submission_enabled"], False)

    def test_controls_persist_value_free_and_replay_immutably(self):
        first = self._set(paused=True, daily_limit=3)
        replay = self._set(paused=True, daily_limit=3)

        self.assertEqual(first["controls"], replay["controls"])
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertFalse(first["controls"]["submission_enabled"])
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "CONTROLLED_APPLICATION_CONTROLS_CHANGED",
            )).scalars())
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]), {
            "profile_id", "application_id", "request_id", "paused",
            "daily_limit", "submission_enabled", "revision",
        })

        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.assertEqual(
            get_controlled_controls(self.store, "fixture", self.application_id)["paused"],
            True,
        )
        with self.assertRaises(RehearsalStoreError) as raised:
            self._set(paused=False, daily_limit=3)
        self.assertEqual(raised.exception.code, "CONTROLLED_CONTROLS_REQUEST_ID_REUSED")

    def test_corrupt_latest_controls_fail_closed_instead_of_restoring_defaults(self):
        self._set(paused=True, daily_limit=3)
        with self.store.engine.begin() as connection:
            event = connection.execute(select(m.audit.c.id, m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "CONTROLLED_APPLICATION_CONTROLS_CHANGED",
            )).mappings().one()
            connection.execute(m.audit.update().where(m.audit.c.id == event["id"]).values(
                payload={**event["payload"], "paused": "false"},
            ))

        with self.assertRaises(RehearsalStoreError) as raised:
            get_controlled_controls(self.store, "fixture", self.application_id)
        self.assertEqual(raised.exception.code, "CONTROLLED_CONTROLS_INVALID")

    def test_revisions_order_settings_when_timestamps_are_equal(self):
        timestamp = "2026-10-17T12:00:00+00:00"
        with patch("services.applications.rehearsal_store.now", return_value=timestamp):
            self._set(request_id="same-time-pause", paused=True, daily_limit=5)
            self._set(request_id="same-time-resume", paused=False, daily_limit=7)

        current = get_controlled_controls(self.store, "fixture", self.application_id)
        self.assertEqual(current["request_id"], "same-time-resume")
        self.assertFalse(current["paused"])
        self.assertEqual(current["daily_limit"], 7)
        with self.store.engine.connect() as connection:
            revisions = list(connection.execute(select(m.audit.c.payload["revision"]).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "CONTROLLED_APPLICATION_CONTROLS_CHANGED",
            ).order_by(m.audit.c.id)).scalars())
        self.assertEqual(sorted(revisions), [1, 2])

    def test_setting_changed_profile_audit_is_atomic_and_idempotent(self):
        response = self._set(request_id="audited-pause", paused=True, daily_limit=4)
        setting_id = identity(
            "controlled-application-setting-changed-v1", "fixture",
            self.application_id, "audited-pause",
        )
        with self.store.engine.connect() as connection:
            event = connection.execute(select(m.audit).where(
                m.audit.c.id == setting_id,
            )).mappings().one()
        self.assertEqual(event["entity_id"], "fixture")
        self.assertEqual(event["action"], "SETTING_CHANGED")
        self.assertEqual(event["payload"], {})
        self.assertFalse(response["replayed"])

        replay = self._set(request_id="audited-pause", paused=True, daily_limit=4)
        self.assertTrue(replay["replayed"])
        with self.store.engine.connect() as connection:
            count = connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.id == setting_id,
            )).scalar_one()
        self.assertEqual(count, 1)

    def test_invalid_control_types_ranges_and_request_ids_are_rejected(self):
        invalid = [
            {"paused": 0, "daily_limit": 10},
            {"paused": False, "daily_limit": True},
            {"paused": False, "daily_limit": -1},
            {"paused": False, "daily_limit": 101},
        ]
        for index, values in enumerate(invalid):
            with self.subTest(values=values):
                with self.assertRaises(RehearsalStoreError):
                    self._set(request_id=f"invalid-{index}", **values)
        with self.assertRaises(RehearsalStoreError):
            set_controlled_controls(
                self.store, "fixture", self.application_id,
                request_id="contains private data !", paused=False, daily_limit=10,
            )

    def test_admission_is_allowed_by_defaults_and_pause_blocks_it(self):
        with self.store.engine.begin() as connection:
            self.assertIsNone(require_controlled_admission(
                connection, "fixture", self.application_id,
            ))

        self._set(paused=True, daily_limit=10)
        with self.store.engine.begin() as connection:
            with self.assertRaises(RehearsalStoreError) as raised:
                require_controlled_admission(
                    connection, "fixture", self.application_id,
                )
        self.assertEqual(raised.exception.code, "CONTROLLED_REHEARSAL_PAUSED")

    def test_daily_admission_limit_is_enforced_and_operation_is_bound(self):
        self._set(paused=False, daily_limit=1)
        self._insert_operation("controlled-operation-1")
        with self.store.engine.begin() as connection:
            require_controlled_admission(connection, "fixture", self.application_id)
            receipt = record_controlled_admission(
                connection, "fixture", self.application_id, "controlled-operation-1",
            )
            replay = record_controlled_admission(
                connection, "fixture", self.application_id, "controlled-operation-1",
            )
        self.assertFalse(receipt["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(receipt["admitted_at"], replay["admitted_at"])
        with self.store.engine.begin() as connection:
            require_controlled_execution(
                connection, "fixture", self.application_id, "controlled-operation-1",
            )
            with self.assertRaises(RehearsalStoreError) as raised:
                require_controlled_admission(connection, "fixture", self.application_id)
        self.assertEqual(raised.exception.code, "CONTROLLED_DAILY_LIMIT_REACHED")

    def test_pause_blocks_execution_after_admission_and_resume_restores_it(self):
        self._set(paused=False, daily_limit=10)
        self._insert_operation("interrupted-operation")
        with self.store.engine.begin() as connection:
            record_controlled_admission(
                connection, "fixture", self.application_id, "interrupted-operation",
            )

        self._set(request_id="pause-run", paused=True, daily_limit=10)
        with self.store.engine.begin() as connection:
            with self.assertRaises(RehearsalStoreError) as raised:
                require_controlled_execution(
                    connection, "fixture", self.application_id, "interrupted-operation",
                )
        self.assertEqual(raised.exception.code, "CONTROLLED_REHEARSAL_PAUSED")

        self._set(request_id="resume-run", paused=False, daily_limit=10)
        with self.store.engine.begin() as connection:
            execution = require_controlled_execution(
                connection, "fixture", self.application_id, "interrupted-operation",
            )
        self.assertEqual(execution["operation_id"], "interrupted-operation")
        self.assertFalse(execution["submission_enabled"])

    def test_execution_rejects_admission_owned_by_another_profile(self):
        self._set(paused=False, daily_limit=10)
        self._insert_operation("owner-operation")
        with self.store.engine.begin() as connection:
            record_controlled_admission(
                connection, "fixture", self.application_id, "owner-operation",
            )
        with self.store.engine.begin() as connection:
            with self.assertRaises(RehearsalStoreError) as raised:
                require_controlled_execution(
                    connection, "other-profile", self.application_id, "owner-operation",
                )
        self.assertEqual(raised.exception.code, "APPLICATION_NOT_FOUND")

    def test_execution_requires_an_admission_for_the_same_owner(self):
        self._set(paused=False, daily_limit=10)
        with self.store.engine.begin() as connection:
            with self.assertRaises(RehearsalStoreError) as raised:
                require_controlled_execution(
                    connection, "fixture", self.application_id, "missing-operation",
                )
        self.assertEqual(raised.exception.code, "CONTROLLED_ADMISSION_REQUIRED")

    def test_execution_rejects_admission_payload_with_extra_or_changed_fields(self):
        self._set(paused=False, daily_limit=10)
        self._insert_operation("tampered-admission")
        with self.store.engine.begin() as connection:
            record_controlled_admission(
                connection, "fixture", self.application_id, "tampered-admission",
            )
        event_id = identity(
            "controlled-application-admission-v1", "fixture",
            self.application_id, "tampered-admission",
        )
        with self.store.engine.begin() as connection:
            event = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.id == event_id,
            )).scalar_one()
            connection.execute(m.audit.update().where(m.audit.c.id == event_id).values(
                payload={**event, "unexpected": True},
            ))

        with self.store.engine.begin() as connection:
            with self.assertRaises(RehearsalStoreError) as raised:
                require_controlled_execution(
                    connection, "fixture", self.application_id, "tampered-admission",
                )
        self.assertEqual(raised.exception.code, "CONTROLLED_ADMISSION_REQUIRED")

    def test_cross_profile_controls_do_not_disclose_or_change_state(self):
        with self.assertRaises(RehearsalStoreError) as raised:
            get_controlled_controls(self.store, "other-profile", self.application_id)
        self.assertEqual(raised.exception.code, "APPLICATION_NOT_FOUND")

        with self.assertRaises(RehearsalStoreError):
            set_controlled_controls(
                self.store, "other-profile", self.application_id,
                request_id="cross-profile", paused=False, daily_limit=10,
            )
        with self.store.engine.connect() as connection:
            count = connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action == "CONTROLLED_APPLICATION_CONTROLS_CHANGED",
            )).scalar_one()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
