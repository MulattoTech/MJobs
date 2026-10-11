"""The local verifier must not report incomplete runtime evidence as PASS."""
import unittest

from cli.verify_readiness import evaluate_readiness, validate_timeout


class CliReadinessTests(unittest.TestCase):
    def test_every_required_runtime_dependency_must_be_explicitly_ready(self):
        checks = {
            "configuration": True,
            "schema": True,
            "database": True,
            "queue": True,
            "scheduler": True,
            "worker": True,
            "workers": {
                "mjobs-application": True,
                "mjobs-company-discovery": True,
                "mjobs-discovery": True,
                "mjobs-document": True,
            },
        }

        result = evaluate_readiness({"status": "ready", "db_backend": "postgresql", "checks": checks})

        self.assertTrue(result["ready"])
        self.assertEqual(result["checks"]["Scheduler healthy"], "PASS")
        self.assertEqual(result["checks"]["Company discovery worker"], "PASS")

    def test_missing_scheduler_and_stale_company_worker_cannot_be_reported_as_pass(self):
        checks = {
            "configuration": True,
            "schema": True,
            "database": True,
            "queue": True,
            "worker": False,
            "workers": {
                "mjobs-application": True,
                "mjobs-discovery": True,
                "mjobs-document": True,
            },
        }

        result = evaluate_readiness({"status": "ready", "checks": checks})

        self.assertFalse(result["ready"])
        self.assertEqual(result["checks"]["Scheduler healthy"], "NOT_RUN")
        self.assertEqual(result["checks"]["Company discovery worker"], "NOT_RUN")

    def test_api_degraded_status_cannot_be_overridden_by_true_check_fields(self):
        checks = {
            "configuration": True,
            "schema": True,
            "database": True,
            "queue": True,
            "scheduler": True,
            "worker": True,
            "workers": {
                "mjobs-application": True,
                "mjobs-company-discovery": True,
                "mjobs-discovery": True,
                "mjobs-document": True,
            },
        }

        result = evaluate_readiness({"status": "degraded", "checks": checks})

        self.assertFalse(result["ready"])

    def test_http_error_cannot_be_overridden_by_ready_body(self):
        checks = {
            "configuration": True,
            "schema": True,
            "database": True,
            "queue": True,
            "scheduler": True,
            "worker": True,
            "workers": {
                "mjobs-application": True,
                "mjobs-company-discovery": True,
                "mjobs-discovery": True,
                "mjobs-document": True,
            },
        }

        result = evaluate_readiness({"status": "ready", "checks": checks}, http_status=503)

        self.assertFalse(result["ready"])

    def test_network_timeout_must_be_finite_and_bounded(self):
        for value in ("nan", "inf", "30.1", "0", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_timeout(value)
        self.assertEqual(validate_timeout("30"), 30.0)

    def test_sqlite_database_liveness_is_not_postgres_evidence(self):
        checks = {
            "configuration": True,
            "schema": True,
            "database": True,
            "queue": True,
            "scheduler": True,
            "worker": True,
            "workers": {
                "mjobs-application": True,
                "mjobs-company-discovery": True,
                "mjobs-discovery": True,
                "mjobs-document": True,
            },
        }

        result = evaluate_readiness({"status": "ready", "db_backend": "sqlite", "checks": checks})

        self.assertFalse(result["ready"])
        self.assertEqual(result["checks"]["PostgreSQL reachable"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
