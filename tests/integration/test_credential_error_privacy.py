"""Unexpected credential failures cannot reflect secrets or escape into logs."""
import asyncio
import gc
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, event, text

from packages.persistence import models as m
from packages.persistence.store import Store
from services.credentials.vault import CredentialCipher, CredentialConfigurationError
from tests.integration import test_provider_credentials_api as fixture
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url


CANARY = "synthetic-private-error-canary"


class CredentialErrorPrivacyTests(unittest.TestCase):
    setUp = fixture.ProviderCredentialApiTests.setUp
    tearDown = fixture.ProviderCredentialApiTests.tearDown

    def assert_safe_failure(self, response):
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": {"code": "CREDENTIAL_STORE_UNAVAILABLE"}})
        self.assertNotIn(CANARY, response.text)

    def save(self):
        response = self.client.put("/profiles/fixture/provider-credentials/openai",
                                   json={"secret": CANARY})
        self.assertEqual(response.status_code, 200, response.text)

    def test_unexpected_cipher_errors_and_unrecognized_codes_are_value_free(self):
        for error in (ValueError(CANARY), RuntimeError(CANARY),
                      CredentialConfigurationError(CANARY)):
            with self.subTest(kind=type(error).__name__):
                with patch.object(CredentialCipher, "encrypt", side_effect=error):
                    response = self.client.put("/profiles/fixture/provider-credentials/openai",
                                               json={"secret": CANARY})
                self.assert_safe_failure(response)
                self.assertEqual(self.store.provider_credentials("fixture"), [])

    def test_failure_after_ciphertext_insert_rolls_back_without_reflecting_details(self):
        attempted = []

        def fail_after_insert(connection, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO provider_credentials"):
                attempted.append(True)
                self.assertNotIn(CANARY, repr(parameters))
                raise ValueError(CANARY + " storage failure after encryption")

        event.listen(self.store.engine, "after_cursor_execute", fail_after_insert)
        try:
            response = self.client.put("/profiles/fixture/provider-credentials/openai",
                                       json={"secret": CANARY})
        finally:
            event.remove(self.store.engine, "after_cursor_execute", fail_after_insert)
        self.assertEqual(attempted, [True])
        self.assert_safe_failure(response)
        self.assertEqual(self.store.provider_credentials("fixture"), [])
        if self.store.engine.dialect.name == "sqlite":
            self.assertNotIn(CANARY.encode(), self.database.read_bytes())

    def test_decrypt_failure_is_private_and_preserves_saved_credential(self):
        self.save()
        with patch.object(CredentialCipher, "decrypt", side_effect=ValueError(CANARY)):
            self.assert_safe_failure(self.client.post(
                "/profiles/fixture/provider-credentials/openai/test"))
        self.assertEqual(len(self.store.provider_credentials("fixture")), 1)

    def test_probe_setup_failure_never_reserves_capacity(self):
        self.save()
        with patch("services.credentials.connection.provider_connection_deadline",
                   side_effect=ValueError(CANARY)):
            self.assert_safe_failure(self.client.post(
                "/profiles/fixture/provider-credentials/openai/test"))
        self.assert_probe_capacity_available()

    def test_read_delete_and_probe_metadata_failures_are_value_free(self):
        self.save()
        for operation, method, path in (
            ("provider_credentials", "get", "/profiles/fixture/provider-credentials"),
            ("delete_provider_credential", "delete", "/profiles/fixture/provider-credentials/openai"),
            ("record_provider_credential_test", "post", "/profiles/fixture/provider-credentials/openai/test"),
        ):
            with self.subTest(operation=operation), \
                    patch.object(self.store, operation, side_effect=ValueError(CANARY)), \
                    patch("services.credentials.connection.test_provider_connection",
                          return_value={"status": "CONNECTED", "code": None}):
                self.assert_safe_failure(getattr(self.client, method)(path))

    def test_unexpected_probe_exception_does_not_escape_and_slot_is_reusable(self):
        self.save()
        with patch("services.credentials.connection.test_provider_connection",
                   side_effect=ValueError(CANARY)):
            self.assert_safe_failure(self.client.post(
                "/profiles/fixture/provider-credentials/openai/test"))
        with patch("services.credentials.connection.test_provider_connection",
                   return_value={"status": "CONNECTED", "code": None}):
            response = self.client.post("/profiles/fixture/provider-credentials/openai/test")
        self.assertEqual(response.status_code, 200, response.text)

    def test_lower_layer_http_exception_does_not_make_private_details_trusted(self):
        for error in (HTTPException(500, CANARY),
                      HTTPException(503, {"code": CANARY}),
                      CredentialConfigurationError([CANARY])):
            with self.subTest(kind=type(error).__name__), \
                    patch.object(CredentialCipher, "encrypt", side_effect=error):
                self.assert_safe_failure(self.client.put(
                    "/profiles/fixture/provider-credentials/openai", json={"secret": CANARY}))

    def _cancel_with_probe_cleanup(self, *, repeated, fail=True):
        self.save()
        app = self.client.app
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "")
                        == "/profiles/{profile_id}/provider-credentials/{provider}/test")
        started, cancellation_observed, release, finished = (threading.Event() for _ in range(4))
        loop_errors = []

        def probe(provider, secret, *, cancellation_event, deadline):
            started.set()
            try:
                if not cancellation_event.wait(3):
                    raise AssertionError("Cancellation did not reach the owned probe")
                cancellation_observed.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release the owned probe")
                if fail:
                    raise ValueError(CANARY)
                return {"status": "CONNECTED", "code": None}
            finally:
                finished.set()

        async def scenario():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
            request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
            task = asyncio.create_task(endpoint("fixture", "openai", request))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                self.assertTrue(await asyncio.to_thread(cancellation_observed.wait, 2))
                if repeated:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
                # The done callback owns slot release and exception retrieval.
                await asyncio.sleep(0)
                gc.collect()
                await asyncio.sleep(0)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        with patch("services.credentials.connection.test_provider_connection", side_effect=probe), \
                patch.object(self.store, "record_provider_credential_test",
                             side_effect=ValueError(CANARY)) as metadata:
            asyncio.run(scenario())
        metadata.assert_not_called()
        self.assertEqual(loop_errors, [])
        self.assertNotIn(CANARY, json.dumps(loop_errors, default=str))
        self.assert_probe_capacity_available()

    def assert_probe_capacity_available(self):
        slots = self.client.app.state.credential_probe_slots
        acquired = 0
        try:
            while slots.acquire(blocking=False):
                acquired += 1
            from apps.api.main import CREDENTIAL_PROBE_MAX_IN_FLIGHT
            self.assertEqual(acquired, CREDENTIAL_PROBE_MAX_IN_FLIGHT)
        finally:
            for _ in range(acquired):
                slots.release()

    def test_cancellation_survives_probe_failure_during_cleanup(self):
        self._cancel_with_probe_cleanup(repeated=False)

    def test_repeated_cancellation_retrieves_late_error_and_releases_slot(self):
        self._cancel_with_probe_cleanup(repeated=True)

    def test_cancellation_prevents_metadata_failure_after_successful_probe_cleanup(self):
        self._cancel_with_probe_cleanup(repeated=False, fail=False)

    def test_cancellation_during_watcher_cleanup_prevents_metadata_publication(self):
        self.save()
        app = self.client.app
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "")
                        == "/profiles/{profile_id}/provider-credentials/{provider}/test")
        watcher_started = threading.Event()

        def probe(*_args, **_kwargs):
            if not watcher_started.wait(2):
                raise AssertionError("Disconnect watcher did not start")
            return {"status": "CONNECTED", "code": None}

        async def scenario():
            cleanup_started = asyncio.Event()
            never = asyncio.Event()

            async def is_disconnected():
                watcher_started.set()
                try:
                    await never.wait()
                except asyncio.CancelledError:
                    # Model a receive scope that absorbs the watcher's own
                    # cancellation while teardown is still in progress.
                    cleanup_started.set()
                    await never.wait()
                return False

            task = asyncio.create_task(endpoint(
                "fixture", "openai", SimpleNamespace(is_disconnected=is_disconnected)))
            try:
                await asyncio.wait_for(cleanup_started.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                never.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with patch("services.credentials.connection.test_provider_connection", side_effect=probe), \
                patch.object(self.store, "record_provider_credential_test",
                             side_effect=ValueError(CANARY)) as metadata:
            asyncio.run(scenario())
        metadata.assert_not_called()
        self.assert_probe_capacity_available()


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Requires explicit disposable loopback MJOBS_TEST_POSTGRES_URL")
class CredentialErrorPrivacyPostgreSQLTests(CredentialErrorPrivacyTests):
    def setUp(self):
        url = _disposable_postgres_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        schema = "mjobs_credential_privacy_" + uuid4().hex
        admin = create_engine(url, hide_parameters=True)
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))

        def drop_schema():
            with admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))

        self.addCleanup(drop_schema)
        database_url = url.update_query_dict({"options": "-csearch_path=" + schema +
            " -clock_timeout=5000 -cstatement_timeout=30000"}).render_as_string(hide_password=False)

        class TestStore(Store):
            def __init__(self, ignored):
                super().__init__(database_url)

            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)

        self.enterContext(patch.object(fixture, "Store", TestStore))
        super().setUp()

    def test_actual_driver_detail_is_not_exposed_by_credential_api(self):
        self.save()

        def driver_error(_profile_id):
            with self.store.engine.begin() as connection:
                # PostgreSQL includes the raised server detail even when
                # SQLAlchemy hides bound parameters. The API must sanitize it.
                connection.execute(text("DO $$ BEGIN RAISE EXCEPTION USING MESSAGE = "
                    "'synthetic-private-error-canary'; END $$"))

        with patch.object(self.store, "provider_credentials", side_effect=driver_error):
            self.assert_safe_failure(self.client.get("/profiles/fixture/provider-credentials"))
        self.assertEqual(len(self.store.provider_credentials("fixture")), 1)
