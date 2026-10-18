"""MJ020/021: reviewed company-feed policy admission and durable readback.

Synthetic disposable PERSONAL store; no network, worker, scheduler or daemon.
The full-policy acceptance oracle intentionally exposes missing API fields.
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from services.company_discovery.monitoring import source_version
from services.company_discovery.public_client import PublicCompanyClient
from tests.integration.test_company_monitoring_recovery import MonitoringFixture


POLICY = {"request_timeout": 5, "rate_limit": 30, "concurrency": 2, "jitter_seconds": 17}


class CompanyMonitoringPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MonitoringFixture()
        self.addCleanup(self.fixture.close)
        blocker = patch.object(PublicCompanyClient, "get", side_effect=AssertionError("Policy review must not fetch"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def client(self, store):
        return TestClient(create_app(store, "http://127.0.0.1:1", self.fixture.root,
                                    "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def review(self, policy):
        payload = {"source_version": source_version(self.fixture.source), "revision": 0,
                   "poll_interval": 900, **policy}
        with self.client(self.fixture.store) as client:
            response = client.put(f"/company-sources/{self.fixture.source['id']}/monitoring", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        reopened = Store(self.fixture.store.url)
        try:
            with self.client(reopened) as fresh:
                rows = fresh.get("/company-sources/view").json()["company_sources"]
                row = next(row for row in rows if row["id"] == self.fixture.source["id"])
                saved = row["monitoring"]
                self.assertEqual(saved["revision"], 1)
                self.assertEqual(saved["poll_interval"], 900)
                for key, value in policy.items():
                    self.assertEqual(saved[key], value)
        finally:
            reopened.engine.dispose()

    def test_legacy_interval_only_review_survives_fresh_store_api_readback(self):
        self.review({})

    def test_company_feed_accepts_configurable_per_source_transport_and_jitter_policy(self):
        self.review(POLICY)

    def test_global_source_accepts_the_same_policy_field_values_without_egress(self):
        with self.client(self.fixture.store) as client:
            response = client.put("/sources", json={"provider": "remotive", "tenant": "default",
                                  "poll_interval": 900, **POLICY})
            self.assertEqual(response.status_code, 200, response.text)

    def test_global_source_retains_one_rpm_admission_without_company_bounds(self):
        with self.client(self.fixture.store) as client:
            response = client.put('/sources', json=dict(provider='remotive', tenant='default',
                                 poll_interval=900, rate_limit=1))
            self.assertEqual(response.status_code, 200, response.text)
            sources = client.get('/sources').json()['sources']
            source = next(row for row in sources if row['provider'] == 'remotive' and row['tenant'] == 'default')
            self.assertEqual(source['rate_limit'], 1)

    def test_strict_policy_rejection_and_partial_legacy_update_preserves_review(self):
        self.review(POLICY)
        with self.client(self.fixture.store) as client:
            path = f"/company-sources/{self.fixture.source['id']}/monitoring"
            base = dict(source_version=source_version(self.fixture.source), revision=1, poll_interval=900)
            for key, value in [('request_timeout', 1), ('request_timeout', 11), ('request_timeout', True),
                               ('request_timeout', None), ('rate_limit', 0), ('rate_limit', 1), ('rate_limit', 2), ('rate_limit', 14), ('rate_limit', 61),
                               ('concurrency', 9), ('jitter_seconds', -1), ('jitter_seconds', 301)]:
                with self.subTest(key=key, value=value):
                    self.assertEqual(client.put(path, json={**base, key: value}).status_code, 422)
            saved = client.put(path, json=base)
            self.assertEqual(saved.status_code, 200, saved.text)
            for key, value in POLICY.items():
                self.assertEqual(saved.json()[key], value)
            self.assertEqual(client.put(path, json=base).status_code, 409, 'stale CAS must fail')
