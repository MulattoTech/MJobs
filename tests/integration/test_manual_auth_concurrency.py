"""Manual windows share durable browser admission with API peers and workers."""
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import os
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.operations.progress import record_operation_progress
from tests.integration.test_manual_auth_session import ManualAuthSessionApiTests
from workers.dispatch import enqueue_operation
from workers.operations import OperationCancelled, admit_inline, claim, finish_inline, recover_expired, request_cancel
from workers.operations import record_manual_auth_process


def legacy_browser_adapter(test):
    """Exercise legacy Playwright mocks without entering the native child host.

    Mounted native admission/closure is covered by test_manual_auth_isolated.
    These tests retain the supported non-Windows adapter's cleanup assertions.
    """
    @wraps(test)
    def run(self):
        from services.applications.mock_adapter import open_mock_authentication
        def legacy(**kwargs):
            for key in ('operation_id', 'owner_token', 'budget_seconds'):
                kwargs.pop(key, None)
            return open_mock_authentication(**kwargs)
        with patch('apps.api.manual_auth.open_mock_authentication', side_effect=legacy):
            return test(self)
    return run


class ManualAuthConcurrencyTests(ManualAuthSessionApiTests):
    def test_api_process_identity_persisted_before_launch_and_private_after_restart(self):
        from services.applications.browser_process import owner_process_status

        def window(**kwargs):
            peer = Store(f"sqlite:///{self.database.as_posix()}")
            try:
                with peer.engine.connect() as connection:
                    row = connection.execute(select(m.operations).where(
                        m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
                owner = row["recovery_metadata"]["manual_auth_owner"]
                self.assertTrue(owner["api_process_identified"])
                self.assertEqual(owner["api_process"]["pid"], os.getpid())
                self.assertEqual(owner_process_status(owner["api_process"]), "LIVE")
                self.assertNotIn("api_process", self.client.get(f"/operations/{row['id']}").text)
                with peer.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == row["id"]).values(
                        lease_expires_at="2000-01-01T00:00:00+00:00"))
                recover_expired(peer)
                with peer.engine.connect() as connection:
                    blocked = connection.execute(select(m.operations).where(
                        m.operations.c.id == row["id"])).mappings().one()
                self.assertEqual(blocked["state"], "BLOCKED")
                self.assertEqual(blocked["recovery_metadata"]["manual_auth_owner"], owner)
                self.assertEqual(owner_process_status(owner["api_process"]), "LIVE")
            finally:
                peer.engine.dispose()
            return "MANUAL_AUTH_WINDOW_CLOSED"

        with patch("apps.api.manual_auth.open_mock_authentication", side_effect=window):
            self.assertEqual(self.client.post(self.url).status_code, 409)

    def test_unavailable_api_process_identity_never_admits_or_launches(self):
        with patch("services.applications.browser_process.capture_owner_identity",
                   side_effect=ValueError("OWNER_IDENTITY_UNAVAILABLE")), \
                patch("apps.api.manual_auth.open_mock_authentication") as launch:
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.store.count(m.operations.name), 0)
        launch.assert_not_called()

    def test_process_identity_is_durable_immutable_private_and_owner_fenced(self):
        identifier = self.admit_owned_window()
        identity = {"pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}
        self.assertFalse(record_manual_auth_process(self.store, identifier, identity, owner_token="b" * 64))
        self.assertTrue(record_manual_auth_process(self.store, identifier, identity, owner_token="a" * 64))
        self.assertTrue(record_manual_auth_process(self.store, identifier, identity, owner_token="a" * 64))
        self.assertFalse(record_manual_auth_process(self.store, identifier, {**identity, "pid": 456},
                                                   owner_token="a" * 64))
        peer = Store(f"sqlite:///{self.database.as_posix()}")
        try:
            with peer.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one()
                audits = connection.execute(select(m.audit).where(m.audit.c.entity_id == identifier,
                    m.audit.c.action == "MANUAL_AUTH_PROCESS_IDENTIFIED")).mappings().all()
            self.assertEqual(row["recovery_metadata"]["manual_auth_owner"]["process"], identity)
            self.assertEqual(len(audits), 1)
            self.assertNotIn("host", self.client.get(f"/operations/{identifier}").text)
            for state in ("LIVE", "UNKNOWN"):
                with patch("services.applications.browser_process.process_status", return_value=state):
                    self.assertFalse(finish_inline(peer, identifier, "COMPLETED", browser_closed=True,
                                                  owner_token="a" * 64))
                with peer.engine.connect() as connection:
                    held = connection.execute(select(m.operations).where(
                        m.operations.c.id == identifier)).mappings().one()
                self.assertEqual((held["state"], held["error"]), ("BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"))
                self.assertEqual(held["recovery_metadata"]["manual_auth_owner"]["process"], identity)
            damaged = dict(held["recovery_metadata"])
            damaged_owner = dict(damaged["manual_auth_owner"])
            damaged_owner.pop("process")
            damaged["manual_auth_owner"] = damaged_owner
            with peer.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                    recovery_metadata=damaged))
            self.assertFalse(finish_inline(peer, identifier, "COMPLETED", browser_closed=True,
                                          owner_token="a" * 64))
            with peer.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                    recovery_metadata=held["recovery_metadata"]))
            with patch("services.applications.browser_process.process_status", return_value="EXITED"):
                self.assertTrue(finish_inline(peer, identifier, "COMPLETED", browser_closed=True,
                                             owner_token="a" * 64))
        finally:
            peer.engine.dispose()

    def admit_owned_window(self, token="a" * 64):
        with self.store.engine.connect() as connection:
            attempt_id = connection.execute(select(m.attempts.c.id).where(
                m.attempts.c.application_id == self.application_id)).scalar_one()
        return admit_inline(self.store, "MANUAL_AUTH", self.application_id, owner_token=token,
                            manual_auth_identity={
                                "profile_id": self.store.application(self.application_id)["profile_id"],
                                "attempt_id": attempt_id})

    def test_owner_token_fences_reopened_store_expired_release_and_duplicate(self):
        token = "b" * 64
        identifier = self.admit_owned_window(token)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                lease_expires_at="2000-01-01T00:00:00+00:00"))
        recover_expired(self.store)
        peer = Store(f"sqlite:///{self.database.as_posix()}")
        try:
            self.assertFalse(finish_inline(peer, identifier, "COMPLETED", browser_closed=True,
                                          owner_token="c" * 64))
            with peer.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one()
            self.assertEqual(row["state"], "BLOCKED")
            self.assertNotIn(token, str(row["recovery_metadata"]))
            self.assertTrue(finish_inline(peer, identifier, "COMPLETED", browser_closed=True,
                                         owner_token=token))
            self.assertFalse(finish_inline(peer, identifier, "COMPLETED", browser_closed=True,
                                          owner_token=token))
            successor = self.admit_owned_window("d" * 64)
            self.assertFalse(finish_inline(peer, successor, "COMPLETED", browser_closed=True,
                                          owner_token=token))
            with peer.engine.connect() as connection:
                events = connection.execute(select(m.audit).where(
                    m.audit.c.entity_id == identifier,
                    m.audit.c.action == "MANUAL_AUTH_OWNER_RELEASED")).mappings().all()
            self.assertEqual(len(events), 1)
            self.assertNotIn(token, str(events))
        finally:
            peer.engine.dispose()

    def test_owned_window_progress_and_public_reads_hide_internal_ownership(self):
        identifier = self.admit_owned_window()
        profile_id = self.store.application(self.application_id)["profile_id"]
        for url in (f"/profiles/{profile_id}/operations/progress", "/operations",
                    f"/operations?profile_id={profile_id}", f"/operations/{identifier}"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("manual_auth_owner", response.text)
                self.assertNotIn("token_sha256", response.text)

    def test_confirmed_closure_cannot_record_a_blocked_release(self):
        identifier = self.admit_owned_window()
        with self.assertRaises(ValueError):
            finish_inline(self.store, identifier, "BLOCKED", "BROWSER_RELEASE_UNCONFIRMED",
                          browser_closed=True, owner_token="a" * 64)

    def test_public_cancellation_hides_owner_and_preserves_owned_capacity(self):
        identifier = self.admit_owned_window()
        before_application = self.store.application(self.application_id)
        attempts = self.store.count("application_attempts")
        with self.store.engine.connect() as connection:
            before = connection.execute(select(m.operations).where(
                m.operations.c.id == identifier)).mappings().one()
            owner = before["recovery_metadata"]["manual_auth_owner"]
        for blocked in (False, True):
            if blocked:
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(
                        m.operations.c.id == identifier).values(
                            lease_expires_at="2000-01-01T00:00:00+00:00"))
                recover_expired(self.store)
            with self.subTest(blocked=blocked):
                response = self.client.post(f"/operations/{identifier}/cancel")
                self.assertEqual(response.status_code, 202, response.text)
                self.assertNotIn("manual_auth_owner", response.text)
                self.assertNotIn("token_sha256", response.text)
                with self.store.engine.connect() as connection:
                    row = connection.execute(select(m.operations).where(
                        m.operations.c.id == identifier)).mappings().one()
                self.assertEqual(row["state"], "BLOCKED" if blocked else "RUNNING")
                self.assertTrue(row["cancel_requested"])
                self.assertEqual(row["recovery_metadata"]["manual_auth_owner"], owner)
                with patch("apps.api.manual_auth.open_mock_authentication") as launch:
                    self.assertEqual(self.client.post(self.url).status_code, 409)
                    launch.assert_not_called()
        self.assertEqual(self.store.application(self.application_id), before_application)
        self.assertEqual(self.store.count("application_attempts"), attempts)
        self.assertTrue(finish_inline(self.store, identifier, "COMPLETED",
                                     browser_closed=True, owner_token="a" * 64))

    def test_api_reports_failed_owner_release_without_claiming_success(self):
        with patch("apps.api.manual_auth.open_mock_authentication", return_value="MANUAL_AUTH_WINDOW_CLOSED"), \
                patch("apps.api.manual_auth.finish_inline", return_value=False):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("recovery", response.json()["detail"])

    def test_progress_update_preserves_private_owner_fence(self):
        identifier = self.admit_owned_window()
        record_operation_progress(self.store, identifier, current_step="PREPARING", progress=2,
                                  recovery_metadata={"action": "MANUAL_REVIEW"})
        self.assertTrue(finish_inline(self.store, identifier, "COMPLETED", browser_closed=True,
                                     owner_token="a" * 64))

    def test_progress_cannot_bypass_manual_auth_owner_release(self):
        identifier = self.admit_owned_window()
        with self.assertRaisesRegex(ValueError, "MANUAL_AUTH_REQUIRES_OWNER_RELEASE"):
            record_operation_progress(self.store, identifier, current_step="COMPLETED", progress=100,
                                      state="COMPLETED")

    def test_corrupt_owner_metadata_fails_closed_without_mutation(self):
        identifier = self.admit_owned_window()
        for metadata in ({}, {"manual_auth_owner": {"token_sha256": "a" * 64}},
                         {"manual_auth_owner": {"token_sha256": "\u2603" * 64,
                                                "profile_id": "fixture", "attempt_id": "attempt"}}):
            with self.subTest(metadata=metadata):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                        recovery_metadata=metadata))
                self.assertFalse(finish_inline(self.store, identifier, "COMPLETED", browser_closed=True,
                                              owner_token="a" * 64))
                with self.store.engine.connect() as connection:
                    row = connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one()
                self.assertEqual(row["state"], "RUNNING")

    def test_bare_closure_flag_cannot_release_another_api_calls_browser(self):
        def window(**kwargs):
            with self.store.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(
                    m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
            peer = Store(f"sqlite:///{self.database.as_posix()}")
            try:
                self.assertFalse(finish_inline(peer, row["id"], "COMPLETED", browser_closed=True))
                with peer.engine.connect() as connection:
                    current = connection.execute(select(m.operations).where(
                        m.operations.c.id == row["id"])).mappings().one()
                self.assertEqual(current["state"], "RUNNING")
                identity = current["recovery_metadata"]["manual_auth_owner"]
                self.assertEqual(identity["profile_id"], self.store.application(self.application_id)["profile_id"])
                self.assertEqual(identity["attempt_id"], kwargs["attempt_id"])
            finally:
                peer.engine.dispose()
            return "MANUAL_AUTH_WINDOW_CLOSED"
        with patch("apps.api.manual_auth.open_mock_authentication", side_effect=window):
            self.assertEqual(self.client.post(self.url).status_code, 200)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual(row["state"], "COMPLETED")

    def test_legacy_unknown_owner_cannot_release_blocked_capacity(self):
        identifier = admit_inline(self.store, "MANUAL_AUTH", self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                lease_expires_at="2000-01-01T00:00:00+00:00"))
        recover_expired(self.store)
        self.assertFalse(finish_inline(self.store, identifier, "COMPLETED", browser_closed=True))
        with patch("apps.api.manual_auth.open_mock_authentication") as launch:
            self.assertEqual(self.client.post(self.url).status_code, 409)
            launch.assert_not_called()

    def test_two_api_instances_reject_duplicate_before_launch_and_allow_explicit_retry(self):
        peer = Store(f"sqlite:///{self.database.as_posix()}")
        client = TestClient(create_app(peer, self.origin, self.root, operation_mode="FIXTURE"))
        entered, release = threading.Event(), threading.Event()

        def window(**kwargs):
            if entered.is_set() and not release.is_set():
                return "MANUAL_AUTH_WINDOW_CLOSED"
            entered.set()
            self.assertTrue(release.wait(10))
            return "MANUAL_AUTH_WINDOW_CLOSED"

        try:
            with patch.dict(os.environ, {"MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "2"}), \
                    patch("apps.api.manual_auth.open_mock_authentication", side_effect=window) as launch:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    first = pool.submit(self.client.post, self.url)
                    try:
                        self.assertTrue(entered.wait(10))
                        busy = client.post(self.url)
                        self.assertEqual(busy.status_code, 409, busy.text)
                        self.assertIn("retry", busy.json()["detail"].lower())
                        self.assertEqual(launch.call_count, 1)
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=10).status_code, 200)
                self.assertEqual(client.post(self.url).status_code, 200)
                self.assertEqual(launch.call_count, 2)
            self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
            self.assertEqual(self.store.count("application_attempts"), 1)
        finally:
            client.close()
            peer.engine.dispose()

    def test_running_worker_blocks_manual_window_and_manual_window_blocks_worker(self):
        worker = enqueue_operation(self.store, "APPLICATION_FORM_REVIEW", self.application_id, {})
        with patch.dict(os.environ, {"MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "1"}):
            self.assertIsNotNone(claim(self.store, worker))
            with patch("apps.api.manual_auth.open_mock_authentication") as launch:
                busy = self.client.post(self.url)
            self.assertEqual(busy.status_code, 409, busy.text)
            launch.assert_not_called()
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == worker).values(state="COMPLETED"))
            manual = self.admit_owned_window()
            other = enqueue_operation(self.store, "APPLICATION_FORM_REVIEW", "different-application", {})
            self.assertIsNone(claim(self.store, other))
            self.assertTrue(finish_inline(self.store, manual, "COMPLETED", browser_closed=True,
                                         owner_token="a" * 64))
            self.assertIsNotNone(claim(self.store, other))

    def test_window_failure_releases_slot_and_crash_recovery_retains_uncertain_capacity(self):
        with patch("apps.api.manual_auth.open_mock_authentication", return_value="OPEN_UNAVAILABLE"):
            self.assertEqual(self.client.post(self.url).status_code, 503)
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual(operation["state"], "FAILED")
        manual = self.admit_owned_window()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == manual).values(lease_expires_at="2000-01-01T00:00:00+00:00"))
        recover_expired(self.store)
        with self.store.engine.connect() as connection:
            recovered = connection.execute(select(m.operations).where(m.operations.c.id == manual)).mappings().one()
        self.assertEqual(recovered["state"], "BLOCKED")
        self.assertEqual(recovered["error"], "BROWSER_RELEASE_UNCONFIRMED")
        with patch("apps.api.manual_auth.open_mock_authentication") as launch:
            self.assertEqual(self.client.post(self.url).status_code, 409)
            launch.assert_not_called()
        # Only the original owning call, after confirmed closure, can release.
        self.assertTrue(finish_inline(self.store, manual, "CANCELLED", "CANCEL_REQUESTED", browser_closed=True,
                                     owner_token="a" * 64))
        with patch("apps.api.manual_auth.open_mock_authentication", return_value="MANUAL_AUTH_WINDOW_CLOSED"):
            self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.count("application_attempts"), 1)

    def test_uncertain_close_retains_capacity_and_prevents_retry(self):
        with patch("apps.api.manual_auth.open_mock_authentication", return_value="CLOSE_UNCONFIRMED"):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503, response.text)
        with patch("apps.api.manual_auth.open_mock_authentication") as launch:
            self.assertEqual(self.client.post(self.url).status_code, 409)
            launch.assert_not_called()
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")

    @legacy_browser_adapter
    def test_browser_and_transport_cleanup_failure_cannot_release_capacity(self):
        with patch("services.applications.browser_process.capture_browser_identity", return_value={
                "pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}), \
                patch("services.applications.browser_process.process_status", return_value="EXITED"), \
                patch("services.applications.manual_auth.MockClient") as client, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            client.return_value.allowed.return_value = True
            client.return_value.close.side_effect = RuntimeError("synthetic transport cleanup failure")
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            page = context.new_page.return_value
            context.pages = [page]
            page.is_closed.return_value = True
            context.close.side_effect = RuntimeError("synthetic browser cleanup failure")
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("synthetic", response.text)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual((row["state"], row["error"]), ("BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"))

    @legacy_browser_adapter
    def test_live_page_wait_error_is_unavailable_after_cleanup_not_successful_close(self):
        from playwright.sync_api import Error
        events = []
        with patch("services.applications.browser_process.capture_browser_identity", return_value={
                "pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}), \
                patch("services.applications.browser_process.process_status", return_value="EXITED"), \
                patch("services.applications.manual_auth.MockClient") as client, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            client.return_value.allowed.return_value = True
            manager = playwright.return_value
            context = manager.__enter__.return_value.chromium.launch_persistent_context.return_value
            page = context.new_page.return_value
            context.pages = [page]
            page.is_closed.return_value = False
            page.wait_for_timeout.side_effect = Error("synthetic private transport diagnostic")
            context.close.side_effect = lambda: events.append("cleanup")
            manager.__exit__.side_effect = lambda *args: events.append("driver_exit")
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"], "Manual sign-in window is unavailable on this runtime")
        self.assertEqual(events, ["cleanup", "driver_exit"])
        with self.store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(
                m.operations.c.kind == "MANUAL_AUTH")).scalar_one()
        self.assertEqual(state, "FAILED")
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.count("application_attempts"), 1)

    @legacy_browser_adapter
    def test_closed_page_with_failed_cleanup_stays_blocked_before_driver_exit(self):
        from playwright.sync_api import Error
        events = []
        with patch("services.applications.browser_process.capture_browser_identity", return_value={
                "pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}), \
                patch("services.applications.browser_process.process_status", return_value="EXITED"), \
                patch("services.applications.manual_auth.MockClient") as client, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            client.return_value.allowed.return_value = True
            manager = playwright.return_value
            context = manager.__enter__.return_value.chromium.launch_persistent_context.return_value
            page = context.new_page.return_value
            context.pages = [page]
            page.is_closed.side_effect = [False, True]
            page.wait_for_timeout.side_effect = Error("synthetic closed page")

            def fail_cleanup():
                events.append("cleanup")
                raise Error("synthetic uncertain context closure")

            context.close.side_effect = fail_cleanup
            manager.__exit__.side_effect = lambda *args: events.append("driver_exit")
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(events, ["cleanup", "driver_exit"])
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual((row["state"], row["error"]), ("BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"))
        with patch("apps.api.manual_auth.open_mock_authentication") as launch:
            self.assertEqual(self.client.post(self.url).status_code, 409)
            launch.assert_not_called()

    @legacy_browser_adapter
    def test_real_helper_cancel_check_cleans_context_before_driver_exit_and_release(self):
        events = []

        def cancel_during_wait(_milliseconds):
            with self.store.engine.connect() as connection:
                identifier = connection.execute(select(m.operations.c.id).where(
                    m.operations.c.kind == "MANUAL_AUTH",
                    m.operations.c.state == "RUNNING")).scalar_one()
            request_cancel(self.store, identifier)

        with patch("services.applications.browser_process.capture_browser_identity", return_value={
                "pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}), \
                patch("services.applications.browser_process.process_status", return_value="EXITED"), \
                patch("services.applications.manual_auth.MockClient") as client, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            client.return_value.allowed.return_value = True
            manager = playwright.return_value
            context = manager.__enter__.return_value.chromium.launch_persistent_context.return_value
            page = context.new_page.return_value
            context.pages = [page]
            page.is_closed.return_value = False
            page.wait_for_timeout.side_effect = cancel_during_wait
            context.close.side_effect = lambda: events.append("cleanup")
            manager.__exit__.side_effect = lambda *args: events.append("driver_exit")
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(events, ["cleanup", "driver_exit"])
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual(row["state"], "CANCELLED")
        self.assertIsNone(row["lease_expires_at"])
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.count("application_attempts"), 1)

    @legacy_browser_adapter
    def test_launch_failure_without_context_retains_capacity_and_blocks_retry(self):
        with patch("services.applications.browser_process.capture_browser_identity", return_value={
                "pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}), \
                patch("services.applications.browser_process.process_status", return_value="EXITED"), \
                patch("services.applications.manual_auth.MockClient") as client, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            client.return_value.allowed.return_value = True
            launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
            launch.side_effect = RuntimeError("synthetic partial launch with private profile path")
            response = self.client.post(self.url)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertNotIn("synthetic", response.text)
            self.assertEqual(self.client.post(self.url).status_code, 409)
            launch.assert_called_once()
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual((row["state"], row["error"]), ("BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"))
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.count("application_attempts"), 1)

    @legacy_browser_adapter
    def test_failure_before_launch_releases_capacity_for_explicit_retry(self):
        with patch("services.applications.browser_process.capture_browser_identity", return_value={
                "pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}), \
                patch("services.applications.browser_process.process_status", return_value="EXITED"), \
                patch("services.applications.manual_auth.MockClient") as client, \
                patch("playwright.sync_api.sync_playwright") as playwright:
            client.return_value.allowed.return_value = True
            playwright.return_value.__enter__.side_effect = RuntimeError("synthetic unavailable runtime")
            self.assertEqual(self.client.post(self.url).status_code, 503)
            playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.assert_not_called()
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual(row["state"], "FAILED")
        with patch("apps.api.manual_auth.open_mock_authentication", return_value="MANUAL_AUTH_WINDOW_CLOSED"):
            self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.count("application_attempts"), 1)

    def test_finish_without_closure_confirmation_cannot_release_crashed_owner(self):
        identifier = admit_inline(self.store, "MANUAL_AUTH", self.application_id)
        self.assertTrue(finish_inline(self.store, identifier, "FAILED"))
        with patch("apps.api.manual_auth.open_mock_authentication") as launch:
            self.assertEqual(self.client.post(self.url).status_code, 409)
            launch.assert_not_called()

    def test_unknown_window_status_retains_capacity(self):
        with patch("apps.api.manual_auth.open_mock_authentication", return_value=None):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503, response.text)
        with patch("apps.api.manual_auth.open_mock_authentication") as launch:
            self.assertEqual(self.client.post(self.url).status_code, 409)
            launch.assert_not_called()

    def test_cancel_closes_controlled_window_before_releasing_admission(self):
        def window(**kwargs):
            with self.store.engine.connect() as connection:
                identifier = connection.execute(select(m.operations.c.id).where(
                    m.operations.c.kind == "MANUAL_AUTH", m.operations.c.state == "RUNNING")).scalar_one()
            request_cancel(self.store, identifier)
            try:
                kwargs["check"]()
            except OperationCancelled:
                # The real helper closes its context before returning this.
                return "OPEN_UNAVAILABLE"
        with patch("apps.api.manual_auth.open_mock_authentication", side_effect=window):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409, response.text)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual(row["state"], "CANCELLED")
        self.assertIsNone(row["lease_expires_at"])
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")

    def test_deadline_releases_admission_without_retry(self):
        with patch("apps.api.manual_auth.monotonic", side_effect=[0, 0, 601]), \
                patch("apps.api.manual_auth.open_mock_authentication", return_value="MANUAL_AUTH_WINDOW_CLOSED"):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("timed out", response.json()["detail"])
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual((row["state"], row["error"]), ("FAILED", "TIMEOUT"))
        self.assertIsNone(row["lease_expires_at"])
        self.assertEqual(self.store.count("application_attempts"), 1)
