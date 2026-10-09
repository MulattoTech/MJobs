"""The application factory exposes the persisted interview-preparation workflow."""
from pathlib import Path
import tempfile
import unittest

from apps.api.main import create_app
from packages.persistence.store import Store


class InterviewPreparationRouterMountTests(unittest.TestCase):
    def test_factory_mounts_scoped_read_and_write_without_submission_route(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-interview-prep-mount-") as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'mount.db').as_posix()}")
            store.migrate()
            try:
                app = create_app(
                    store,
                    "http://127.0.0.1:1",
                    Path(directory),
                    "redis://127.0.0.1:1/0",
                    operation_mode="FIXTURE",
                )
            finally:
                store.engine.dispose()

        routes = app.openapi()["paths"]
        resource = "/profiles/{profile_id}/jobs/{job_id}/interview-prep-packs"

        self.assertIn(resource, routes)
        self.assertIn("get", routes[resource])
        self.assertIn("post", routes[resource])
        self.assertFalse(
            any("interview-prep-packs" in path and "submit" in path for path in routes),
            "Interview preparation must not expose an application submission route.",
        )


if __name__ == "__main__":
    unittest.main()
