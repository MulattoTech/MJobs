"""Profile-scoped connected readback for the company discovery queue."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation


class CompanyDiscoveryQueueApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'queue.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self.store.save_profile({**PROFILE, "id": "other-profile", "name": "Other Profile"})
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(id="company-a", name="Northwind Systems", domain=""))
            connection.execute(m.companies.insert().values(id="company-b", name="Private Employer", domain=""))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_queue_is_profile_scoped_and_returns_only_safe_status_fields(self):
        enqueue_operation(self.store, "COMPANY_DISCOVER", "company-a", {
            "profile_id": "fixture", "candidate": {"company_name": "Northwind Systems", "email": "private@example.invalid"},
            "trigger_reason": "USER_ADDED_COMPANY", "dispatch_priority": "HIGH",
        })
        enqueue_operation(self.store, "COMPANY_DISCOVER", "company-b", {
            "profile_id": "other-profile", "candidate": {"company_name": "Private Employer"},
            "trigger_reason": "USER_ADDED_COMPANY",
        })

        response = self.client.get("/profiles/fixture/company-discovery/queue")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["profile_id"], "fixture")
        self.assertEqual(payload["counts"]["queued"], 1)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["company"], "Northwind Systems")
        self.assertEqual(item["state"], "QUEUED")
        self.assertEqual(item["priority"], "HIGH")
        self.assertEqual(set(item), {"request_id", "company", "state", "priority", "updated_at", "attempt_count"})
        serialized = response.text.casefold()
        for value in ("private employer", "private@example.invalid", '"candidate"', '"error"'):
            self.assertNotIn(value, serialized)

    def test_missing_profile_and_invalid_limit_have_bounded_errors(self):
        missing = self.client.get("/profiles/missing/company-discovery/queue")
        invalid_limit = self.client.get("/profiles/fixture/company-discovery/queue?limit=0")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid_limit.status_code, 422)


if __name__ == "__main__":
    unittest.main()
