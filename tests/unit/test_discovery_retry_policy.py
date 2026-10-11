"""Automatic retries cannot override application or source-monitor authority."""
import unittest
from unittest.mock import Mock

import httpx

from services.discovery.sources import greenhouse, remotive, remoteok, wwr
from workers.retries import discovery_retry_plan


class DiscoveryRetryPolicyTests(unittest.TestCase):
    def operation(self, **changes):
        return {"kind": "DISCOVER", "params": {}, "attempt_count": 1, **changes}

    def test_registered_provider_errors_share_explicit_retry_cooldown_contract(self):
        for provider in (greenhouse, remotive, remoteok, wwr):
            with self.subTest(provider=provider.__name__):
                error = provider.SourceError("SOURCE_SERVER_ERROR", retry_seconds=600)
                plan = discovery_retry_plan(Mock(), self.operation(), error, "2026-10-11T00:00:00+00:00")
                self.assertEqual(plan["next_attempt_at"], "2026-10-11T00:10:00+00:00")
                self.assertEqual(plan["error"], "SOURCE_SERVER_ERROR")

    def test_automatic_retries_exclude_irreversible_and_existing_monitor_workflows(self):
        for kind, params in (("APPLY_MOCK", {}), ("PREPARE", {}), ("AI_EVALUATE", {}),
                             ("APPLICATION_FORM_REVIEW", {}), ("COMPANY_SOURCE_REFRESH", {}),
                             ("COMPANY_DISCOVER", {"monitoring_source_id": "source"})):
            with self.subTest(kind=kind):
                connection = Mock()
                self.assertIsNone(discovery_retry_plan(connection, self.operation(kind=kind, params=params),
                    httpx.ReadTimeout("private diagnostic"), "2026-10-11T00:00:00+00:00"))
                connection.execute.assert_not_called()

    def test_unsafe_provider_failures_and_invalid_delays_are_never_retried(self):
        for error in (remotive.SourceError("SOURCE_UNAVAILABLE"),
                      wwr.SourceError("SOURCE_PAYLOAD_INVALID"),
                      greenhouse.SourceError("SUBMISSION_UNCONFIRMED"),
                      remoteok.SourceError("SOURCE_TIMEOUT", retry_seconds=-1),
                      remotive.SourceError("SOURCE_TIMEOUT", retry_seconds=31536001)):
            with self.subTest(code=error.code, delay=error.retry_seconds):
                self.assertIsNone(discovery_retry_plan(Mock(), self.operation(), error,
                    "2026-10-11T00:00:00+00:00"))
