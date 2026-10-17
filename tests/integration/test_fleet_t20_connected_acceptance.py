"""Team 20 API-to-durable-store release acceptance, using synthetic data only."""
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.dispatch import dispatch_pending


def test_public_company_discovery_intent_survives_queue_outage_and_replay():
    """Exercise the public API, durable outbox, interruption, replay and readback."""
    with tempfile.TemporaryDirectory(prefix="mjobs-t20-company-acceptance-") as directory:
        root = Path(directory)
        database_url = f"sqlite:///{(root / 'acceptance.db').as_posix()}"
        store = Store(database_url)
        store.migrate()
        client = None
        try:
            store.bind_workspace_mode("PERSONAL")
            store.save_profile({**PROFILE, "id": "t20-synthetic"})
            client = TestClient(create_app(
                store,
                "http://127.0.0.1:8090",
                root,
                "redis://127.0.0.1:1/0",
                operation_mode="PERSONAL",
            ))
            intent = {
                "company_name": "Synthetic Example Systems",
                "company_website": "https://employer.example/",
                "reason": "USER_ADDED_COMPANY",
                "profile_id": "t20-synthetic",
                "request_id": "t20-company-intent-1",
            }
            accepted = client.post("/companies/discovery", json=intent)
            assert accepted.status_code == 202, accepted.text
            response = accepted.json()
            assert response["status"] == "QUEUED"
            operation_id = response["operation_id"]

            # A missing Redis service is an interruption. It must leave exactly
            # one retryable, durable operation rather than lose or duplicate intent.
            dispatch = dispatch_pending(store, "redis://127.0.0.1:1/0")
            assert dispatch["status"] == "QUEUE_UNAVAILABLE"
            replay = client.post("/companies/discovery", json=intent)
            assert replay.status_code == 202, replay.text
            assert replay.json()["operation_id"] == operation_id

            # Reusing an idempotency key for a different intent must fail closed.
            conflict = client.post("/companies/discovery", json={
                **intent,
                "company_name": "Different Synthetic Employer",
            })
            assert conflict.status_code == 409

            assert client.get("/settings").json()["external_submission_enabled"] is False
            fresh = Store(database_url)
            try:
                with fresh.engine.connect() as connection:
                    operation = connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id,
                    )).mappings().one()
                assert operation["state"] == "QUEUED"
                assert operation["params"]["fetch_mode"] == "PUBLIC_HTTPS"
                assert operation["params"]["request_id"] == intent["request_id"]
                assert fresh.count("worker_operations") == 1
            finally:
                fresh.engine.dispose()
        finally:
            if client is not None:
                client.close()
            store.engine.dispose()
