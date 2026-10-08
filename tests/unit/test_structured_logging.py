import json
from io import StringIO
import unittest

from workers import structured_logging
from workers.structured_logging import log_operation_finished


class StructuredOperationLoggingTests(unittest.TestCase):
    def test_configured_worker_output_is_one_json_line(self):
        configure = getattr(structured_logging, "configure_operation_logger", None)
        self.assertTrue(callable(configure), "worker operation logger must use a JSON-only output handler")
        logger = structured_logging._LOGGER
        prior_handlers = list(logger.handlers)
        prior_level = logger.level
        prior_propagate = logger.propagate
        output = StringIO()
        try:
            configure(output)
            log_operation_finished({"id": "operation-1", "kind": "DISCOVER", "params": {}},
                                   "COMPLETED", None, 0.01)
            line = output.getvalue().strip()
            record = json.loads(line)
            self.assertEqual(record["event"], "operation_finished")
            self.assertEqual(record["operation_id"], "operation-1")
            self.assertEqual(record["level"], "INFO")
            self.assertTrue(line.startswith("{"))
            self.assertTrue(line.endswith("}"))
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers = prior_handlers
            logger.setLevel(prior_level)
            logger.propagate = prior_propagate

    def test_discovery_logs_include_only_the_persisted_source_identifier(self):
        cases = (
            ("DISCOVER", {"source_id": "source-123"}, "source-123"),
            ("COMPANY_DISCOVER", {"monitoring_source_id": "source-456"}, "source-456"),
            ("COMPANY_SOURCE_REFRESH", {"source_id": "source-789"}, "source-789"),
        )
        for kind, params, expected_source_id in cases:
            with self.subTest(kind=kind), self.assertLogs("mjobs.operations", level="INFO") as captured:
                log_operation_finished({
                    "id": "operation-1", "kind": kind, "entity_id": "entity-1",
                    "params": {**params, "candidate": {"company_name": "private company"},
                               "api_key": "synthetic-secret-value"},
                }, "COMPLETED", None, 0.01)

            message = captured.records[0].getMessage()
            record = json.loads(message)
            self.assertEqual(record.get("source_id"), expected_source_id)
            self.assertNotIn("params", record)
            self.assertNotIn("candidate", record)
            self.assertNotIn("private company", message)
            self.assertNotIn("synthetic-secret-value", message)

    def test_discovery_omits_missing_malformed_and_secret_shaped_source_identifiers(self):
        cases = (
            ("missing", {}),
            ("malformed", {"source_id": "https://example.test/jobs?token=private"}),
            ("secret-shaped", {"source_id": "api-key-secret-value"}),
        )
        for case, params in cases:
            with self.subTest(case=case), self.assertLogs("mjobs.operations", level="INFO") as captured:
                log_operation_finished({
                    "id": "operation-1", "kind": "DISCOVER", "params": params,
                }, "COMPLETED", None, 0.01)

            message = captured.records[0].getMessage()
            record = json.loads(message)
            self.assertNotIn("source_id", record)
            self.assertNotIn("private", message)
            self.assertNotIn("api-key-secret-value", message)

    def test_prepare_log_contains_safe_context_but_never_serializes_params(self):
        operation = {
            "id": "a" * 64,
            "kind": "PREPARE",
            "entity_id": "profile-id",
            "params": {
                "job_id": "job-123",
                "source_id": "source-must-not-appear-for-prepare",
                "api_key": "synthetic-secret-value",
                "tailor_fact_rewrites": [{"text": "private resume content"}],
            },
        }

        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            log_operation_finished(operation, "DEAD_LETTERED", "DOCUMENT_GENERATION_FAILED", 0.025)

        message = captured.records[0].getMessage()
        record = json.loads(message)
        self.assertEqual(record["operation_id"], "a" * 64)
        self.assertEqual(record["operation_kind"], "PREPARE")
        self.assertEqual(record["job_id"], "job-123")
        self.assertEqual(record["worker"], "mjobs-worker")
        self.assertEqual(record["result"], "DEAD_LETTERED")
        self.assertEqual(record["error_code"], "DOCUMENT_GENERATION_FAILED")
        self.assertNotIn("source_id", record)
        self.assertEqual(record["duration_ms"], 25)
        self.assertTrue(record["timestamp"].endswith("Z"))
        self.assertNotIn("params", record)
        self.assertNotIn("api_key", message)
        self.assertNotIn("synthetic-secret-value", message)
        self.assertNotIn("private resume content", message)

    def test_untrusted_identifier_and_error_text_are_omitted(self):
        operation = {
            "id": "operation-1",
            "kind": "PREPARE",
            "params": {"job_id": "job-id\napi_key=synthetic-secret"},
        }

        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            log_operation_finished(operation, "DEAD_LETTERED", "provider said secret-value", 0)

        message = captured.records[0].getMessage()
        record = json.loads(message)
        self.assertNotIn("job_id", record)
        self.assertNotIn("error_code", record)
        self.assertNotIn("synthetic-secret", message)
        self.assertNotIn("secret-value", message)


if __name__ == "__main__":
    unittest.main()
