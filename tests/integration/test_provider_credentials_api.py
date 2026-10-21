"""Encrypted, profile-scoped provider credential API behavior."""
import asyncio
import base64
import copy
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class ProviderCredentialApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "credentials.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(copy.deepcopy(PROFILE))
        second = copy.deepcopy(PROFILE)
        second.update(id="other-profile", name="Other Profile", email="other@example.test")
        self.store.save_profile(second)
        self.key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
        self.environment = patch.dict("os.environ", {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": self.key})
        self.environment.start()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_provider_secret_is_encrypted_masked_profile_scoped_and_deletable(self):
        secret = "synthetic-provider-token-never-return-this"
        created = self.client.put(
            "/profiles/fixture/provider-credentials/openai",
            json={"secret": secret},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["profile_id"], "fixture")
        self.assertEqual(created.json()["provider"], "openai")
        self.assertTrue(created.json()["configured"])
        self.assertEqual(created.json()["masked"], "********")
        self.assertEqual(created.json()["rotation_count"], 0)
        self.assertNotIn(secret, created.text)

        listed = self.client.get("/profiles/fixture/provider-credentials")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["credentials"]), 1)
        self.assertEqual(listed.json()["credentials"][0]["provider"], "openai")
        self.assertNotIn(secret, listed.text)
        self.assertEqual(self.client.get("/profiles/other-profile/provider-credentials").json()["credentials"], [])
        self.assertNotIn(secret.encode("utf-8"), self.database.read_bytes())
        backup = self.root / "synthetic-database-backup.db"
        shutil.copyfile(self.database, backup)
        self.assertNotIn(secret.encode("utf-8"), backup.read_bytes())

        rotated = self.client.put(
            "/profiles/fixture/provider-credentials/openai",
            json={"secret": "synthetic-rotated-provider-token"},
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(rotated.json()["rotation_count"], 1)
        self.assertNotIn("synthetic-rotated-provider-token", rotated.text)
        self.assertNotIn("synthetic-provider-token-never-return-this", self.database.read_text(encoding="latin1"))

        deleted = self.client.delete("/profiles/fixture/provider-credentials/openai")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(self.client.get("/profiles/fixture/provider-credentials").json()["credentials"])

    def test_connection_check_is_explicit_bounded_and_persists_only_sanitized_outcome(self):
        from unittest.mock import patch

        self.client.put("/profiles/fixture/provider-credentials/openai",
                        json={"secret": "synthetic-never-return-this"})
        with patch("services.credentials.connection.test_provider_connection",
                   return_value={"status": "CONNECTED", "code": None}) as check:
            response = self.client.post("/profiles/fixture/provider-credentials/openai/test")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "CONNECTED")
        self.assertNotIn("synthetic-never-return-this", response.text)
        check.assert_called_once()
        args, kwargs = check.call_args
        self.assertEqual(args, ("openai", "synthetic-never-return-this"))
        self.assertIsInstance(kwargs.get("cancellation_event"), threading.Event)
        self.assertFalse(kwargs["cancellation_event"].is_set())
        metadata = self.client.get("/profiles/fixture/provider-credentials").json()["credentials"][0]
        self.assertEqual(metadata["last_test_status"], "CONNECTED")
        self.assertIsNotNone(metadata["last_tested_at"])

    def test_request_disconnect_cancels_inflight_probe_and_persists_safe_classification(self):
        self.client.put("/profiles/fixture/provider-credentials/openai",
                        json={"secret": "synthetic-disconnect-secret"})
        started = threading.Event()
        completed = threading.Event()

        def blocked_probe(provider, secret, *, cancellation_event=None):
            self.assertEqual(provider, "openai")
            self.assertEqual(secret, "synthetic-disconnect-secret")
            self.assertIsInstance(cancellation_event, threading.Event)
            started.set()
            if not cancellation_event.wait(2):
                raise AssertionError("request disconnect did not cancel the provider probe")
            completed.set()
            return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_CANCELLED"}

        async def send_disconnected_request():
            request_sent = False
            disconnected = asyncio.Event()
            messages = []

            async def receive():
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": "/profiles/fixture/provider-credentials/openai/test",
                "raw_path": b"/profiles/fixture/provider-credentials/openai/test",
                "query_string": b"", "root_path": "", "headers": [(b"host", b"testserver")],
                "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
                "state": {},
            }
            task = asyncio.create_task(self.client.app(scope, receive, send))
            try:
                for _ in range(200):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set(), "probe did not start")
                disconnected.set()
                await asyncio.wait_for(task, timeout=2)
                return messages
            finally:
                disconnected.set()
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        with patch("services.credentials.connection.test_provider_connection",
                   side_effect=blocked_probe):
            messages = asyncio.run(send_disconnected_request())

        self.assertTrue(completed.is_set())
        response_body = b"".join(message.get("body", b"") for message in messages)
        self.assertIn(b"CREDENTIAL_PROVIDER_CANCELLED", response_body)
        self.assertNotIn(b"synthetic-disconnect-secret", response_body)
        metadata = self.client.get("/profiles/fixture/provider-credentials").json()["credentials"][0]
        self.assertEqual(metadata["last_test_status"], "FAILED")
        self.assertIsNotNone(metadata["last_tested_at"])
        self.assertNotIn("synthetic-disconnect-secret", repr(metadata))

    def test_missing_encryption_key_fails_closed_and_validation_never_echoes_secret(self):
        self.client.close()
        self.environment.stop()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        secret = "synthetic-secret-must-not-appear-in-errors"
        unavailable = self.client.put("/profiles/fixture/provider-credentials/openai", json={"secret": secret})
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn(secret, unavailable.text)
        invalid = self.client.put("/profiles/fixture/provider-credentials/openai", json={"secret": "", "extra": secret})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(secret, invalid.text)

    def test_ciphertext_survives_store_reopen_and_only_server_connection_check_reads_it(self):
        secret = "synthetic-restart-provider-secret"
        saved = self.client.put("/profiles/fixture/provider-credentials/anthropic",
                                json={"secret": secret})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.client.close()
        self.store.engine.dispose()

        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        with patch("services.credentials.connection.test_provider_connection",
                   return_value={"status": "CONNECTED", "code": None}) as check:
            tested = self.client.post("/profiles/fixture/provider-credentials/anthropic/test")
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertEqual(tested.json()["status"], "CONNECTED")
        self.assertNotIn(secret, tested.text)
        check.assert_called_once()
        args, kwargs = check.call_args
        self.assertEqual(args, ("anthropic", secret))
        self.assertIsInstance(kwargs.get("cancellation_event"), threading.Event)
        listed = self.client.get("/profiles/fixture/provider-credentials")
        self.assertEqual(listed.json()["credentials"][0]["last_test_status"], "CONNECTED")
        self.assertNotIn(secret, listed.text)


if __name__ == "__main__":
    unittest.main()
