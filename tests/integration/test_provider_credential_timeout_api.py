"""Profile-scoped provider credential API timeout classification."""
import base64
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from services.credentials import connection
from tests.integration.test_spine import PROFILE


class ProviderCredentialTimeoutApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'credentials.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(copy.deepcopy(PROFILE))
        self.key = base64.urlsafe_b64encode(
            b"0123456789abcdef0123456789abcdef"
        ).decode("ascii")
        self.environment = patch.dict(
            "os.environ", {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": self.key}
        )
        self.environment.start()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_timeout_is_sanitized_and_persisted_through_profile_api(self):
        secret = "synthetic-provider-secret-do-not-return"
        saved = self.client.put(
            "/profiles/fixture/provider-credentials/openai",
            json={"secret": secret},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        requests = []

        def timeout_transport(request):
            requests.append(request)
            raise httpx.ReadTimeout(
                "synthetic upstream detail api.openai.com secret=" + secret,
                request=request,
            )

        def client_factory(**kwargs):
            kwargs["transport"] = httpx.MockTransport(timeout_transport)
            return httpx.Client(**kwargs)

        actual_check = connection.test_provider_connection

        def invoke_actual_check(provider, credential, *, cancellation_event=None):
            return actual_check(provider, credential, client_factory=client_factory,
                                cancellation_event=cancellation_event)

        # Route the API's normal call through the actual check function with a
        # synthetic transport; every attempted request is intercepted locally.
        with patch(
            "services.credentials.connection.test_provider_connection",
            side_effect=invoke_actual_check,
        ):
            response = self.client.post(
                "/profiles/fixture/provider-credentials/openai/test"
            )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["profile_id"], "fixture")
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["code"], "CREDENTIAL_PROVIDER_TIMEOUT")
        self.assertIsNotNone(result["last_tested_at"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(str(requests[0].url), "https://api.openai.com/v1/models")
        self.assertEqual(requests[0].headers["authorization"], f"Bearer {secret}")

        metadata = self.client.get(
            "/profiles/fixture/provider-credentials"
        )
        self.assertEqual(metadata.status_code, 200, metadata.text)
        credential = metadata.json()["credentials"][0]
        self.assertEqual(credential["last_test_status"], "FAILED")
        self.assertIsNotNone(credential["last_tested_at"])

        for body in (response.text, metadata.text):
            self.assertNotIn(secret, body)
            self.assertNotIn("api.openai.com", body)
            self.assertNotIn("synthetic upstream detail", body)
            self.assertNotIn("ReadTimeout", body)


if __name__ == "__main__":
    unittest.main()
