"""T20 connected acceptance for API readiness and safe degraded deployment state."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from cli.verify_readiness import evaluate_readiness
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class FleetT20ProductJourneyTests(unittest.TestCase):
    def test_api_readiness_and_fixture_data_readback_fail_closed_without_runtime(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-fleet-t20-api-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'journey.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE, id="t20-fixture"))

            with fixture_server() as fixture:
                app = create_app(
                    store,
                    fixture,
                    root,
                    redis_url="redis://127.0.0.1:1/0",
                    operation_mode="FIXTURE",
                )
                with TestClient(app) as client:
                    readiness = client.get("/readiness")
                    self.assertEqual(readiness.status_code, 503)
                    payload = readiness.json()
                    self.assertEqual(payload["status"], "degraded")
                    self.assertEqual(payload["db_backend"], "sqlite")
                    self.assertTrue(payload["checks"]["configuration"])
                    self.assertTrue(payload["checks"]["database"])
                    self.assertFalse(payload["checks"]["queue"])

                    decision = evaluate_readiness(payload, http_status=readiness.status_code)
                    self.assertFalse(decision["ready"])
                    self.assertEqual(decision["checks"]["PostgreSQL reachable"], "NOT_RUN")

                    settings = client.get("/settings")
                    self.assertEqual(settings.status_code, 200)
                    self.assertFalse(settings.json()["external_submission_enabled"])
                    self.assertEqual(
                        client.get("/profiles/t20-fixture").json()["name"],
                        PROFILE["name"],
                    )

            store.engine.dispose()
            reopened = Store(database_url)
            try:
                self.assertEqual(reopened.profile("t20-fixture")["email"], PROFILE["email"])
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
