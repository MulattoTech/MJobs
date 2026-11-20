"""Characterize the application-factory boundary before API extraction."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store


class ApiMainCharacterizationTests(unittest.TestCase):
    def _app(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-api-main-characterization-")
        root = Path(directory.name)
        store = Store(f"sqlite:///{(root / 'api.db').as_posix()}")
        store.migrate()
        app = create_app(store, "http://127.0.0.1:1", root, operation_mode="PERSONAL")
        return directory, store, app

    @staticmethod
    def _route_methods(app):
        return {
            (method.upper(), path)
            for path, operations in app.openapi()["paths"].items()
            for method in operations
            if method != "parameters"
        }

    def test_import_has_no_global_application_singleton(self):
        import apps.api.main as main

        self.assertFalse(hasattr(main, "app"))

    def test_factory_registers_boundary_routes_and_initializes_state(self):
        directory, store, app = self._app()
        try:
            routes = self._route_methods(app)
            self.assertTrue({
                ("GET", "/health"),
                ("GET", "/readiness"),
                ("GET", "/ready"),
                ("GET", "/settings"),
                ("POST", "/discovery"),
                ("GET", "/operations"),
                ("POST", "/profiles/{profile_id}/applications/{application_id}/multipage-state"),
            } <= routes)
            self.assertTrue(hasattr(app.state, "credential_probe_slots"))
            store.assert_workspace_mode("PERSONAL")
            middleware_names = {middleware.cls.__name__ for middleware in app.user_middleware}
            self.assertEqual(
                {"TrustedHostMiddleware", "LocalOriginMiddleware"} <= middleware_names,
                True,
            )
        finally:
            store.engine.dispose()
            directory.cleanup()

    def test_health_settings_and_origin_guard_contract(self):
        directory, store, app = self._app()
        try:
            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(
                    health.json(),
                    {
                        "status": "ok",
                        "check": "liveness",
                        "mode": "LOCAL_FIRST",
                        "operation_mode": "PERSONAL",
                        "external_submission_enabled": False,
                    },
                )
                self.assertEqual(client.get("/settings").json()["external_submission_enabled"], False)
                self.assertEqual(
                    client.get("/health", headers={"Origin": "https://foreign.example"}).status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/profiles", headers={"Origin": "https://foreign.example"}).status_code,
                    403,
                )
                self.assertEqual(
                    client.post("/discovery", headers={"Origin": "https://foreign.example"}).status_code,
                    403,
                )
                self.assertEqual(health.headers["x-content-type-options"], "nosniff")
                self.assertEqual(health.headers["referrer-policy"], "no-referrer")
        finally:
            store.engine.dispose()
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
