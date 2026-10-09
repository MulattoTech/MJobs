"""The production API app registers the durable multi-page application route."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store


class ApplicationMultipageRouterMountTests(unittest.TestCase):
    def test_create_app_registers_profile_scoped_page_state_route(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-multipage-router-") as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'router.db').as_posix()}")
            store.migrate()
            app = create_app(store, "http://127.0.0.1:1", Path(directory),
                             operation_mode="FIXTURE")
            with TestClient(app) as client:
                response = client.get(
                    "/profiles/missing-profile/applications/missing-application/multipage-state"
                )
            store.engine.dispose()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": {"code": "APPLICATION_NOT_FOUND"}})


if __name__ == "__main__":
    unittest.main()
