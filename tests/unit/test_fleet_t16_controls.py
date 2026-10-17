"""Team 16: connected fixture consent and durable value-free audit contracts.

These checks stop at durable admission; they never start an ATS browser.
"""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.applications import controlled_dry_run as service
from workers.operations import OperationCancelled
from tests.integration import test_controlled_application_dry_run as baseline


class FixtureConsentTests(unittest.TestCase):
    _new_store = baseline.ControlledApplicationDryRunTests._new_store
    setUp = baseline.ControlledApplicationDryRunTests.setUp
    tearDown = baseline.ControlledApplicationDryRunTests.tearDown
    path = baseline.ControlledApplicationDryRunTests.path
    review_and_queue = baseline.ControlledApplicationDryRunTests.review_and_queue
    execute = baseline.ControlledApplicationDryRunTests.execute

    def _controls(self, request_id, paused=False, daily_limit=10):
        response = self.client.put(self.path() + "/controls", json={
            "request_id": request_id, "paused": paused, "daily_limit": daily_limit,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_cancelled_admission_counts_towards_limit_and_replay_is_free(self):
        self._controls("t16-limit-one", daily_limit=1)
        preview, body, queued = self.review_and_queue("t16-limited-run")
        replay = self.client.post(self.path() + "/runs", json=body)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["operation_id"], queued["operation_id"])
        run_path = self.path() + "/runs/" + body["request_id"]
        cancelled = self.client.post(run_path + "/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.store.engine.dispose()
        retry = {"request_id": "t16-limited-retry", "fingerprint": preview["fingerprint"],
                 "acknowledged": True}
        denied = self.client.post(run_path + "/retry", json=retry)
        self.assertEqual(denied.status_code, 409, denied.text)
        self._controls("t16-limit-two", daily_limit=2)
        allowed = self.client.post(run_path + "/retry", json=retry)
        self.assertEqual(allowed.status_code, 202, allowed.text)
        self.assertNotEqual(allowed.json()["operation_id"], queued["operation_id"])
        self.assertEqual(self.client.get(run_path).json()["state"], "CANCELLED")

    def test_pause_after_admission_blocks_worker_before_browser_and_preserves_run(self):
        _, body, queued = self.review_and_queue("t16-pause-worker")
        self._controls("t16-pause-before-worker", paused=True)
        with patch.object(service, "run_browser", side_effect=AssertionError("browser forbidden")) as browser:
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        browser.assert_not_called()
        self.store.engine.dispose()
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(saved["state"], "BLOCKED")
        self.assertIsNone(saved["receipt"])
        self._controls("t16-resume-worker", paused=False)
        self.assertEqual(self.client.get(self.path() + "/runs/" + body["request_id"]).json()["state"], "BLOCKED")
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_saved_receipt_recovery_does_not_reopen_preparation_after_pause(self):
        _, body, queued = self.review_and_queue("t16-receipt-recovery")
        # A synthetic saved receipt isolates recovery from browser execution.
        receipt = {"operation_id": queued["operation_id"], "context": "CONTROLLED_FIXTURE",
                   "submission_enabled": False}
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id=identity(service.KIND, queued["operation_id"]),
                entity_id=self.application_id, action=service.KIND,
                timestamp=now(), payload=receipt,
            ))
        self._controls("t16-pause-after-receipt", paused=True)
        with patch.object(service, "run_browser", side_effect=AssertionError("receipt must recover")) as browser:
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        browser.assert_not_called()
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(saved["receipt"], receipt)

    def test_pause_during_preparation_prevents_publication_of_receipt(self):
        self._controls("t16-initial-unpaused")
        _, body, queued = self.review_and_queue("t16-pause-interruption")
        paused_responses = []

        def synthetic_browser(*args, **kwargs):
            # This is a deterministic interruption probe, not browser evidence.
            paused_responses.append(self.client.put(self.path() + "/controls", json={
                "request_id": "t16-pause-during-work", "paused": True, "daily_limit": 10,
            }))
            kwargs["check"]()
            return {"documents": [], "pages": []}

        with patch.object(service, "run_browser", synthetic_browser):
            result = self.execute(queued["operation_id"])
        self.assertEqual(len(paused_responses), 1)
        self.assertEqual(paused_responses[0].status_code, 200, paused_responses[0].text)
        self.assertEqual(result["status"], "BLOCKED", result)
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(saved["error_code"], "CONTROLLED_DRY_RUN_PAUSED")
        self.assertIsNone(saved["receipt"])
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_pause_checkpoint_wrapper_preserves_worker_deadline(self):
        _, _, queued = self.review_and_queue("t16-worker-budget")
        timers = []

        def synthetic_browser(*args, **kwargs):
            timers.append(getattr(kwargs["check"], "remaining_seconds", None))
            raise OperationCancelled("Synthetic deadline probe finished")

        with patch.object(service, "run_browser", synthetic_browser):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "CANCELLED", result)
        self.assertEqual(len(timers), 1)
        self.assertTrue(callable(timers[0]), "Pause wrapper lost the operation deadline")

    def test_pause_after_browser_return_is_rechecked_in_receipt_transaction(self):
        self._controls("t16-initial-publication-controls")
        _, body, queued = self.review_and_queue("t16-publication-fence")
        original_lock = service.lock_scope
        reached = []
        browser_finished = []

        def synthetic_browser(*args, **kwargs):
            browser_finished.append(True)
            return {"documents": [], "pages": []}

        def pause_before_publication_lock(connection, profile_id, application_id):
            # The initial browser check already completed. A control edit that
            # commits before the receipt transaction must still prevent receipt.
            if browser_finished:
                reached.append(True)
                self._controls("t16-pause-before-publication", paused=True)
            return original_lock(connection, profile_id, application_id)

        with patch.object(service, "run_browser", synthetic_browser), \
                patch.object(service, "lock_scope", pause_before_publication_lock):
            result = self.execute(queued["operation_id"])
        self.assertTrue(reached)
        self.assertEqual(result["status"], "BLOCKED", result)
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertIsNone(saved["receipt"])

    def test_controls_reload_strict_input_and_immutable_replay(self):
        path = self.path() + "/controls"
        initial = self.client.get(path)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertFalse(initial.json()["paused"])
        self.assertFalse(initial.json()["submission_enabled"])
        body = {"request_id": "t16-pause-1", "paused": True, "daily_limit": 2}
        for invalid in ({"paused": "true"}, {"paused": 1}, {"daily_limit": True},
                        {"daily_limit": -1}, {"daily_limit": 101},
                        {"submission_enabled": True}, {"password": "PRIVATE_SENTINEL"}):
            response = self.client.put(path, json={**body, **invalid})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("PRIVATE_SENTINEL", response.text)
        changed = self.client.put(path, json=body)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.store.engine.dispose()
        saved = self.client.get(path)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["paused"])
        self.assertEqual(saved.json()["daily_limit"], 2)
        self.assertFalse(saved.json()["submission_enabled"])
        self.assertEqual(self.client.put(path, json=body).status_code, 200)
        conflict = self.client.put(path, json={**body, "paused": False})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        foreign_path = path.replace("/profiles/fixture/", "/profiles/other/")
        self.assertEqual(self.client.get(foreign_path).status_code, 404)
        self.assertEqual(self.client.put(foreign_path, json=body).status_code, 404)
        resumed = self.client.put(path, json={**body, "request_id": "t16-later-resume", "paused": False})
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(self.client.put(path, json=body).status_code, 200)
        self.assertFalse(self.client.get(path).json()["paused"], "Old replay reapplied stale pause")

    def test_control_changes_appear_in_user_audit_without_submission_claims(self):
        def setting_events():
            response = self.client.get("/profiles/fixture/audit-history")
            self.assertEqual(response.status_code, 200, response.text)
            events = response.json()["events"]
            self.assertFalse(any(event["type"] in {
                "APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED",
            } for event in events))
            return [event for event in events if event["type"] == "SETTING_CHANGED"]

        before = setting_events()
        self._controls("t16-audit-pause", paused=True)
        self.store.engine.dispose()
        changed = setting_events()
        self.assertEqual(len(changed), len(before) + 1)
        self._controls("t16-audit-pause", paused=True)
        self.assertEqual(setting_events(), changed)
        self._controls("t16-audit-resume", paused=False)
        self.assertEqual(len(setting_events()), len(before) + 2)

    def test_corrupt_latest_control_does_not_restore_unpaused_defaults(self):
        self._controls("t16-corrupt-pause", paused=True)
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.audit.c.id, m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "CONTROLLED_APPLICATION_CONTROLS_CHANGED",
            )).mappings().one()
            connection.execute(update(m.audit).where(m.audit.c.id == row["id"]).values(
                payload={**row["payload"], "paused": "false"},
            ))
        self.store.engine.dispose()
        response = self.client.get(self.path() + "/controls")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn('"paused":false', response.text)

    def test_latest_pause_survives_backward_wall_clock(self):
        with patch("services.applications.rehearsal_store.now", return_value="2030-01-01T00:00:00+00:00"):
            self._controls("t16-clock-first", paused=False)
        with patch("services.applications.rehearsal_store.now", return_value="2020-01-01T00:00:00+00:00"):
            self._controls("t16-clock-second", paused=True)
        saved = self.client.get(self.path() + "/controls")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["paused"], "A newer pause was lost after clock rollback")

    def test_pause_blocks_admission_and_resume_preserves_review_requirement(self):
        preview = self.client.get(self.path()).json()
        body = {"request_id": "t16-paused-run", "fingerprint": preview["fingerprint"]}
        review = self.client.post(self.path() + "/reviews", json={
            **body, "acknowledged": True,
        })
        self.assertEqual(review.status_code, 200, review.text)
        controls = self.path() + "/controls"
        paused = self.client.put(controls, json={
            "request_id": "t16-pause-admission", "paused": True, "daily_limit": 2,
        })
        self.assertEqual(paused.status_code, 200, paused.text)
        denied = self.client.post(self.path() + "/runs", json=body)
        self.assertEqual(denied.status_code, 409, denied.text)
        with self.store.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(m.operations.c.id).where(
                m.operations.c.kind == "APPLICATION_DRY_RUN",
            )).first())
        resumed = self.client.put(controls, json={
            "request_id": "t16-resume-admission", "paused": False, "daily_limit": 2,
        })
        self.assertEqual(resumed.status_code, 200, resumed.text)
        queued = self.client.post(self.path() + "/runs", json=body)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_explicit_consent_is_required_and_survives_connection_reopen(self):
        preview = self.client.get(self.path()).json()
        body = {"request_id": "t16-run", "fingerprint": preview["fingerprint"]}
        self.assertEqual(self.client.post(self.path() + "/runs", json=body).status_code, 409)
        for consent in (False, 0, 1, "true", None):
            response = self.client.post(self.path() + "/reviews", json={
                **body, "acknowledged": consent,
            })
            self.assertEqual(response.status_code, 422, response.text)
        reviewed = self.client.post(self.path() + "/reviews", json={
            **body, "acknowledged": True,
        })
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.store.engine.dispose()
        reloaded = self.client.get(self.path()).json()
        self.assertEqual(reloaded["latest_review"]["request_id"], body["request_id"])
        self.assertFalse(reloaded["submission_enabled"])
        queued = self.client.post(self.path() + "/runs", json=body)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.store.engine.dispose()
        readback = self.client.get(self.path() + "/runs/" + body["request_id"])
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["operation_id"], queued.json()["operation_id"])
        self.assertIsNone(readback.json()["receipt"])
        replay = self.client.post(self.path() + "/runs", json=body)
        self.assertEqual(replay.json()["operation_id"], queued.json()["operation_id"])

    def test_audit_and_admission_metadata_exclude_values_and_owner_mismatch(self):
        _, body, queued = self.review_and_queue("t16-private-boundary")
        with self.store.engine.connect() as connection:
            audit = [dict(row) for row in connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REHEARSAL_REVIEWED",
            )).mappings()]
            operation = connection.execute(select(m.operations.c.params).where(
                m.operations.c.id == queued["operation_id"],
            )).scalar_one()
        self.assertEqual(len(audit), 1)
        self.assertEqual(set(audit[0]["payload"]), {
            "receipt_version", "profile_id", "application_id", "request_id",
            "fingerprint", "profile_version", "route_id", "acknowledged",
        })
        serialized = json.dumps({"audit": audit, "params": operation})
        self.assertNotIn("Yes, weekends are available.", serialized)
        self.assertNotIn("alex@example.test", serialized)
        other = self.path().replace("/profiles/fixture/", "/profiles/other/")
        for endpoint in ("/reviews", "/runs"):
            response = self.client.post(other + endpoint, json={
                **body, **({"acknowledged": True} if endpoint == "/reviews" else {}),
            })
            self.assertEqual(response.status_code, 404, response.text)
        denied = self.client.get(other + "/runs/" + body["request_id"])
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assertEqual(self.store.count("application_attempts"), 0)
