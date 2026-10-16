"""Unit tests for recovery history persistence and readback."""
import unittest

from packages.persistence import models as m
from packages.persistence.recovery_history import (
    insert_recovery_history,
    read_recovery_history,
)
from packages.persistence.store import Store


class RecoveryHistoryUnitTests(unittest.TestCase):
    def setUp(self):
        self.store = Store("sqlite:///:memory:")
        self.store.migrate()
        with self.store.engine.begin() as conn:
            conn.execute(m.profiles.insert().values(
                id="prof-1", name="Synthetic User", email="user@example.test", version="v1",
            ))
            conn.execute(m.companies.insert().values(
                id="comp-1", name="Synthetic Corp", domain="synthetic.test",
            ))

    def test_read_recovery_history_with_job_not_in_backup_and_target_job_none(self):
        payload = {
            "target_company_id": "comp-1",
            "target_job_id": None,
            "reference_status": "JOB_NOT_IN_BACKUP",
            "state": "UNKNOWN",
            "reasons": ["USER_PREFERRED_COMPANY"],
            "metadata_status": "COMPLETE",
            "authorization_required": True,
            "revision": 1,
            "created_at": "2026-10-15T00:00:00Z",
            "last_seen_at": "2026-10-15T00:00:00Z",
            "historical_refs": {"profile_id": "prof-1"},
        }
        with self.store.engine.begin() as conn:
            insert_recovery_history(conn, "prof-1", [{
                "kind": "company_discovery_request",
                "source_identity": "source-1",
                "schema_version": 1,
                "payload": payload,
                "created_at": "2026-10-15T00:00:00Z",
            }])

        with self.store.engine.connect() as conn:
            rows = read_recovery_history(conn, "prof-1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "company_discovery_request")
        self.assertEqual(rows[0]["source_identity"], "source-1")
        self.assertEqual(rows[0]["payload"]["target_company_id"], "comp-1")
        self.assertIsNone(rows[0]["payload"]["target_job_id"])
        self.assertEqual(rows[0]["payload"]["reference_status"], "JOB_NOT_IN_BACKUP")

    def test_read_recovery_history_rejects_unowned_or_nonexistent_company(self):
        payload = {
            "target_company_id": "foreign-unregistered-comp",
            "target_job_id": None,
            "reference_status": "JOB_NOT_IN_BACKUP",
            "state": "UNKNOWN",
            "reasons": ["USER_PREFERRED_COMPANY"],
            "metadata_status": "COMPLETE",
            "authorization_required": True,
            "revision": 1,
            "created_at": "2026-10-15T00:00:00Z",
            "last_seen_at": "2026-10-15T00:00:00Z",
            "historical_refs": {"profile_id": "prof-1"},
        }
        with self.store.engine.begin() as conn:
            insert_recovery_history(conn, "prof-1", [{
                "kind": "company_discovery_request",
                "source_identity": "source-foreign",
                "schema_version": 1,
                "payload": payload,
                "created_at": "2026-10-15T00:00:00Z",
            }])

        with self.store.engine.connect() as conn:
            with self.assertRaisesRegex(ValueError, "corrupt recovery history company reference"):
                read_recovery_history(conn, "prof-1")


if __name__ == "__main__":
    unittest.main()
