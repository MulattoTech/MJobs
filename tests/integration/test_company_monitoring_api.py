"""Strict API validation and authorization invalidation for source monitoring."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.monitoring import source_version
from services.company_discovery.public_client import PublicCompanyClient
from tests.integration.test_company_monitoring_recovery import MonitoringFixture
from workers.tasks import execute_operation


class CompanyMonitoringApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MonitoringFixture()
        self.store = self.fixture.store

    def tearDown(self):
        self.fixture.close()

    def client(self, store=None, mode="PERSONAL"):
        return TestClient(create_app(
            store or self.store, "http://127.0.0.1:8090", self.fixture.root,
            "redis://127.0.0.1:1/0", operation_mode=mode))

    def monitoring_payload(self, revision=0, version=None):
        return {"source_version": version or source_version(self.fixture.source),
                "revision": revision, "poll_interval": 3600}

    def test_monitoring_input_is_strict_and_rejects_bad_source_hash(self):
        with self.client() as client:
            invalid = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring",
                                 json={"source_version": "bad", "revision": 0, "poll_interval": 3600})
            self.assertEqual(invalid.status_code, 422)
            extra = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring",
                               json={**self.monitoring_payload(), "unexpected": True})
            self.assertEqual(extra.status_code, 422)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.fixture.source["id"])
                                   .values(endpoint="https://employer.example/retargeted.json"))
            stale = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring",
                               json=self.monitoring_payload())
        self.assertEqual(stale.status_code, 409)
        self.assertIn("MONITOR_SOURCE_VERSION_CONFLICT", stale.text)

    def test_stale_monitor_revision_is_rejected(self):
        with self.client() as client:
            first = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring",
                               json=self.monitoring_payload())
            self.assertEqual(first.status_code, 200)
            stale = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring",
                               json=self.monitoring_payload(revision=0))
        self.assertEqual(stale.status_code, 409)
        self.assertIn("MONITOR_REVISION_CONFLICT", stale.text)

    def test_fixture_mode_rejects_monitoring_and_refresh(self):
        temporary = tempfile.TemporaryDirectory()
        fixture_store = Store(f"sqlite:///{(Path(temporary.name) / 'fixture.db').as_posix()}")
        fixture_store.migrate()
        fixture_store.bind_workspace_mode("FIXTURE")
        try:
            with self.client(fixture_store, "FIXTURE") as client:
                monitoring = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring",
                                        json=self.monitoring_payload())
                refresh = client.post(f"/company-sources/{self.fixture.source['id']}/refresh",
                                      json={"profile_id": "fixture"})
            self.assertEqual(monitoring.status_code, 409)
            self.assertEqual(refresh.status_code, 409)
        finally:
            fixture_store.engine.dispose()
            temporary.cleanup()

    def test_enable_disable_cycle_invalidates_queued_revision_before_egress(self):
        with self.client() as client:
            configured = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring",
                                    json=self.monitoring_payload())
            self.assertEqual(configured.status_code, 200)
            queued = client.post(f"/company-sources/{self.fixture.source['id']}/refresh",
                                 json={"profile_id": "fixture"})
            self.assertEqual(queued.status_code, 202)
            self.assertEqual(client.put(f"/company-sources/{self.fixture.source['id']}/enabled",
                                        json={"enabled": False}).status_code, 200)
            self.assertEqual(client.put(f"/company-sources/{self.fixture.source['id']}/enabled",
                                        json={"enabled": True}).status_code, 200)
        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "get", side_effect=AssertionError("unexpected egress")):
            result = execute_operation(queued.json()["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_OPERATION_SUPERSEDED")


if __name__ == "__main__":
    unittest.main()
