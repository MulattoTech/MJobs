"""Manual windows share durable browser admission with API peers and workers."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_manual_auth_session import ManualAuthSessionApiTests
from workers.dispatch import enqueue_operation
from workers.operations import OperationCancelled, admit_inline, claim, finish_inline, recover_expired, request_cancel


class ManualAuthConcurrencyTests(ManualAuthSessionApiTests):
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
            manual = admit_inline(self.store, "MANUAL_AUTH", self.application_id)
            other = enqueue_operation(self.store, "APPLICATION_FORM_REVIEW", "different-application", {})
            self.assertIsNone(claim(self.store, other))
            self.assertTrue(finish_inline(self.store, manual, "COMPLETED", browser_closed=True))
            self.assertIsNotNone(claim(self.store, other))

    def test_window_failure_releases_slot_and_crash_recovery_retains_uncertain_capacity(self):
        with patch("apps.api.manual_auth.open_mock_authentication", return_value="OPEN_UNAVAILABLE"):
            self.assertEqual(self.client.post(self.url).status_code, 503)
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(m.operations.c.kind == "MANUAL_AUTH")).mappings().one()
        self.assertEqual(operation["state"], "FAILED")
        manual = admit_inline(self.store, "MANUAL_AUTH", self.application_id)
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
        self.assertTrue(finish_inline(self.store, manual, "CANCELLED", "CANCEL_REQUESTED", browser_closed=True))
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

    def test_browser_and_transport_cleanup_failure_cannot_release_capacity(self):
        with patch("services.applications.manual_auth.MockClient") as client, \
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

    def test_launch_failure_without_context_retains_capacity_and_blocks_retry(self):
        with patch("services.applications.manual_auth.MockClient") as client, \
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

    def test_failure_before_launch_releases_capacity_for_explicit_retry(self):
        with patch("services.applications.manual_auth.MockClient") as client, \
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
