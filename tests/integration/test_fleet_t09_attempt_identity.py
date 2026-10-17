"""T09 acceptance for concurrent replay identity and interrupted admission."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import attempt_history
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class FleetT09AttemptIdentityTests(unittest.TestCase):
    def test_concurrent_duplicate_identity_is_durable_and_fences_interrupted_retry(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t09-attempt-") as directory, fixture_server() as origin:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'fixture.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = deepcopy(PROFILE)
            profile["id"] = "fixture"
            store.save_profile(profile)
            discover(store, origin, "fixture")
            application_id = prepare(store, store.jobs("fixture")[0]["id"], "fixture", root)

            def reserve():
                connection_store = Store(database_url)
                try:
                    application = connection_store.application(application_id)
                    with connection_store.engine.connect() as connection:
                        route = dict(connection.execute(select(m.routes).where(
                            m.routes.c.id == application["route_id"])).mappings().one())
                        documents = list(connection.execute(select(m.documents).where(
                            m.documents.c.application_id == application_id,
                            m.documents.c.version == application["document_version"])).mappings())
                    return attempt_history.start_attempt(
                        connection_store, application, route, documents,
                        request_id="t09-parallel-request")
                finally:
                    connection_store.engine.dispose()

            barrier = threading.Barrier(2)

            def compete():
                barrier.wait(timeout=10)
                try:
                    return reserve()
                except attempt_history.AttemptConflict as error:
                    return error.code

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(lambda _: compete(), range(2)))
                admitted = [value for value in outcomes if isinstance(value, dict)]
                self.assertEqual(len(admitted), 1, outcomes)
                self.assertFalse(admitted[0]["replayed"])
                self.assertEqual(outcomes.count("APPLICATION_ATTEMPT_IN_PROGRESS"), 1)
                self.assertEqual(store.count("application_attempts"), 1)

                expected_id = attempt_history.attempt_id_for_request(
                    application_id, "t09-parallel-request")
                self.assertEqual(admitted[0]["id"], expected_id)
                store.engine.dispose()

                reopened = Store(database_url)
                try:
                    application = reopened.application(application_id)
                    read_app = create_app(reopened, origin, root, operation_mode="FIXTURE")
                    with TestClient(read_app) as client:
                        response = client.get(
                            f"/profiles/fixture/applications/{application_id}/attempt-history")
                    self.assertEqual(response.status_code, 200, response.text)
                    history = response.json()
                    self.assertEqual(len(history["attempts"]), 1)
                    self.assertEqual(history["attempts"][0]["attempt_id"], expected_id)
                    self.assertEqual(history["attempts"][0]["state"], "RUNNING")

                    with reopened.engine.connect() as connection:
                        route = dict(connection.execute(select(m.routes).where(
                            m.routes.c.id == application["route_id"])).mappings().one())
                        documents = list(connection.execute(select(m.documents).where(
                            m.documents.c.application_id == application_id,
                            m.documents.c.version == application["document_version"])).mappings())
                    with self.assertRaisesRegex(
                            attempt_history.AttemptConflict,
                            "APPLICATION_ATTEMPT_IN_PROGRESS"):
                        attempt_history.start_attempt(
                            reopened, application, route, documents,
                            request_id="t09-parallel-request")
                    self.assertEqual(reopened.count("application_attempts"), 1)
                finally:
                    reopened.engine.dispose()
            finally:
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
