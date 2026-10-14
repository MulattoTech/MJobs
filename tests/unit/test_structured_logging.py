import json
from io import StringIO
import unittest

from workers import structured_logging
from workers.structured_logging import log_operation_finished


class StructuredOperationLoggingTests(unittest.TestCase):
    def test_document_ai_and_review_outcomes_emit_only_fixed_context(self):
        for kind in ("RESUME_EXTRACT_QUEUED", "UPLOADED_RESUME_COMPARE_QUEUED",
                     "AI_EVALUATE_QUEUED", "APPLICATION_FORM_REVIEW", "APPLICATION_DRY_RUN"):
            for result in ("COMPLETED", "BLOCKED", "CANCELLED", "DEAD_LETTERED"):
                with self.subTest(kind=kind, result=result), \
                        self.assertLogs("mjobs.operations", level="INFO") as captured:
                    log_operation_finished({
                        "id": "operation-123", "kind": kind,
                        "entity_id": "private-resume-id", "job_id": "private-job-id",
                        "params": {"resume_text": "SYNTHETIC_PRIVATE_RESUME",
                                   "api_key": "SYNTHETIC_PRIVATE_KEY"},
                    }, result, "INPUT_INVALID", .025)
                self.assertEqual(len(captured.records), 1)
                message = captured.records[0].getMessage()
                record = json.loads(message)
                self.assertEqual(record["operation_id"], "operation-123")
                self.assertEqual(record["operation_kind"], kind)
                self.assertEqual(record["result"], result)
                self.assertEqual(record["duration_ms"], 25)
                self.assertEqual(record["error_code"], "INPUT_INVALID")
                for marker in ("SYNTHETIC_PRIVATE", "private-resume-id", "private-job-id", "params"):
                    self.assertNotIn(marker, message)

    def test_all_identifier_fields_reject_recognized_secret_shapes(self):
        for kind, field, secret in (
            ("DISCOVER", "operation_id", "api-key-SYNTHETIC-CREDENTIAL"),
            ("PREPARE", "job_id", "token-SYNTHETIC-CREDENTIAL"),
            ("COMPANY_DISCOVER", "company_id", "password-SYNTHETIC-CREDENTIAL"),
            ("APPLY_MOCK", "application_id", "credential-SYNTHETIC-CREDENTIAL"),
        ):
            operation = {"id": "operation-1", "kind": kind,
                         "entity_id": "entity-1", "params": {"job_id": "job-1"}}
            if field == "operation_id":
                operation["id"] = secret
            elif field == "job_id":
                operation["params"]["job_id"] = secret
            else:
                operation["entity_id"] = secret
            with self.subTest(field=field), self.assertLogs("mjobs.operations", level="INFO") as captured:
                log_operation_finished(operation, "BLOCKED", "API_KEY_SYNTHETIC_CREDENTIAL", 0)
            record = json.loads(captured.records[0].getMessage())
            self.assertIsNone(record.get(field))
            self.assertNotIn("error_code", record)
            self.assertNotIn("SYNTHETIC", captured.records[0].getMessage())

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

    def test_apply_mock_logs_only_a_known_persisted_ats_provider(self):
        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            log_operation_finished({
                "id": "operation-apply", "kind": "APPLY_MOCK",
                "entity_id": "application-123",
                "job_id": "job-456", "company_id": "company-789",
                "ats_provider": "greenhouse",
                "params": {"submit": True, "ats": "https://host.test/?token=private",
                           "private_answer": "private application data"},
            }, "COMPLETED", None, 0.01)

        message = captured.records[0].getMessage()
        record = json.loads(message)
        self.assertEqual(record["application_id"], "application-123")
        self.assertEqual(record["job_id"], "job-456")
        self.assertEqual(record["company_id"], "company-789")
        self.assertEqual(record.get("ats"), "greenhouse")
        self.assertNotIn("params", record)
        self.assertNotIn("submit", message)
        self.assertNotIn("host.test", message)
        self.assertNotIn("token=private", message)
        self.assertNotIn("private application data", message)

    def test_apply_mock_omits_malformed_or_secret_shaped_job_and_company_ids(self):
        cases = (("bad\njob", "company-1", None, "company-1"),
                 ("job-1", "api-key-secret-value", "job-1", None))
        for job_id, company_id, expected_job_id, expected_company_id in cases:
            with self.subTest(job_id=job_id, company_id=company_id), \
                    self.assertLogs("mjobs.operations", level="INFO") as captured:
                log_operation_finished({
                    "id": "operation-apply", "kind": "APPLY_MOCK",
                    "entity_id": "application-123", "job_id": job_id,
                    "company_id": company_id, "ats_provider": "greenhouse",
                    "params": {"answer": "private application data"},
                }, "BLOCKED", "SAFE_ERROR", 0.01)

            message = captured.records[0].getMessage()
            record = json.loads(message)
            self.assertEqual(record.get("job_id"), expected_job_id)
            self.assertEqual(record.get("company_id"), expected_company_id)
            self.assertNotIn("api-key-secret-value", message)
            self.assertNotIn("private application data", message)

    def test_apply_mock_omits_unknown_malformed_and_secret_shaped_ats_values(self):
        for provider in (None, "unknown-provider", "https://ats.test/?api_key=private",
                         "api-key-secret-value", "greenhouse\npassword=private"):
            with self.subTest(provider=provider), self.assertLogs("mjobs.operations", level="INFO") as captured:
                log_operation_finished({
                    "id": "operation-apply", "kind": "APPLY_MOCK",
                    "ats_provider": provider,
                    "entity_id": "application-123", "params": {"submit": True},
                }, "BLOCKED", "SAFE_ERROR", 0.01)

            message = captured.records[0].getMessage()
            record = json.loads(message)
            self.assertNotIn("ats", record)
            self.assertNotIn("private", message)
            self.assertNotIn("ats.test", message)

    def test_other_operation_kinds_never_log_ats_context(self):
        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            log_operation_finished({
                "id": "operation-prepare", "kind": "PREPARE", "params": {"job_id": "job-1"},
                "ats_provider": "greenhouse",
            }, "COMPLETED", None, 0.01)

        self.assertNotIn("ats", json.loads(captured.records[0].getMessage()))

    def test_company_source_refresh_logs_only_validated_company_id_context(self):
        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            log_operation_finished({
                "id": "operation-refresh", "kind": "COMPANY_SOURCE_REFRESH",
                "entity_id": "source-123", "company_id": "company-456",
                "params": {
                    "source_id": "source-123", "company_name": "Private Employer",
                    "endpoint": "https://private.test/jobs?token=synthetic-secret",
                    "tenant": "private-tenant", "provider": "greenhouse",
                },
            }, "COMPLETED", None, 0.01)

        message = captured.records[0].getMessage()
        record = json.loads(message)
        self.assertEqual(record["company_id"], "company-456")
        self.assertEqual(record["source_id"], "source-123")
        self.assertNotIn("company_name", record)
        self.assertNotIn("provider", record)
        self.assertNotIn("tenant", record)
        self.assertNotIn("params", record)
        for private_value in ("Private Employer", "private.test", "synthetic-secret",
                              "private-tenant", "greenhouse"):
            self.assertNotIn(private_value, message)

    def test_company_id_context_is_refresh_only_and_rejects_malformed_or_secret_shaped_ids(self):
        for kind, company_id, expected in (
            ("COMPANY_SOURCE_REFRESH", "company-456", "company-456"),
            ("COMPANY_SOURCE_REFRESH", None, None),
            ("COMPANY_SOURCE_REFRESH", "bad\ncompany", None),
            ("COMPANY_SOURCE_REFRESH", "api-key-secret-value", None),
            ("COMPANY_DISCOVER", "company-456", "entity-original"),
            ("PREPARE", "company-456", None),
        ):
            with self.subTest(kind=kind, company_id=company_id), \
                    self.assertLogs("mjobs.operations", level="INFO") as captured:
                log_operation_finished({
                    "id": "operation-context", "kind": kind, "entity_id": "entity-original",
                    "company_id": company_id, "params": {},
                }, "BLOCKED", "SAFE_ERROR", 0.01)

            record = json.loads(captured.records[0].getMessage())
            self.assertEqual(record.get("company_id"), expected)

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
