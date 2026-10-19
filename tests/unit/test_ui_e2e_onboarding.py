import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from cli import ui_e2e


class UIE2EOnboardingGuardTests(unittest.TestCase):
    def test_terminal_failure_captures_only_bounded_public_attempt_history(self):
        operation_id = "a" * 64
        history = {
            "profile_id": "private-profile",
            "application_id": "private-application",
            "attempts": [
                {"state": "FAILED", "current_step": "NAVIGATE",
                 "error_code": "PLAYWRIGHT_TIMEOUT",
                 "steps": ["NAVIGATE:STARTED", "private-value=secret"]},
                *[{"state": "CONFIRMED", "current_step": "VERIFY_CONFIRMATION",
                   "error_code": None, "steps": ["VERIFY_CONFIRMATION:COMPLETED"],
                   "browser_session": "private-session"} for _ in range(8)],
            ],
            "has_more": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            journal = ui_e2e.EvidenceJournal(
                target, {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "abc123"}})
            journal.complete({"action": "Earlier synthetic checks", "result": "PASS"})
            journal.begin("Submit to local mock")
            with self.assertRaises(ui_e2e.MockRunTerminalError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda _path: {"operation_id": operation_id,
                                   "state": "DEAD_LETTERED",
                                   "error_code": "DATABASE_POOL_TIMEOUT"},
                    "/mock-runs/synthetic-request", operation_id,
                    attempt_history_reader=lambda: history,
                )
            journal.fail(caught.exception)
            saved = json.loads(target.read_text(encoding="utf-8"))
        summary = saved["failure"]["terminal_operation"]["attempt_history"]
        self.assertEqual(saved["checks"][0]["result"], "PASS")
        self.assertEqual(saved["checks"][1]["result"], "FAIL")
        self.assertEqual(saved["remaining_state"], "NOT_RUN")
        self.assertEqual(summary["attempt_count"], 9)
        self.assertTrue(summary["has_more"])
        self.assertEqual(len(summary["attempts"]), 5)
        self.assertEqual(summary["attempts"][0], {
            "state": "FAILED", "current_step": "NAVIGATE",
            "error_code": "PLAYWRIGHT_TIMEOUT",
            "steps": ["NAVIGATE:STARTED"],
        })
        encoded = json.dumps(summary)
        for private in ("private-profile", "private-application", "private-session",
                        "private-value", "secret"):
            self.assertNotIn(private, encoded)
        self.assertLessEqual(len(encoded.encode("utf-8")), ui_e2e._ATTEMPT_HISTORY_MAX_BYTES)

    def test_attempt_history_reader_runs_only_after_terminal_failure(self):
        calls = []
        result = ui_e2e.wait_for_mock_run(
            lambda _path: {"operation_id": "c" * 64, "state": "COMPLETED"},
            "/mock-runs/synthetic-request", "c" * 64,
            attempt_history_reader=lambda: calls.append("read"),
        )
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(calls, [])

    def test_attempt_history_capture_fail_closed_and_redacts_untrusted_fields(self):
        operation_id = "b" * 64
        for history in (
            {"attempts": [{"state": "PRIVATE STATE", "current_step": "secret/path",
                           "error_code": "SECRET_TOKEN", "steps": ["password:secret"]}]},
            {"attempts": "not-a-list", "profile_id": "private-profile"},
        ):
            with self.subTest(history=history), self.assertRaises(ui_e2e.MockRunTerminalError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda _path: {"operation_id": operation_id,
                                   "state": "DEAD_LETTERED",
                                   "error_code": "TIMEOUT_UNCLASSIFIED"},
                    "/mock-runs/synthetic-request", operation_id,
                    attempt_history_reader=lambda: history,
                )
            summary = caught.exception.terminal_summary["attempt_history"]
            encoded = json.dumps(summary)
            self.assertNotIn("SECRET_TOKEN", encoded)
            self.assertNotIn("private-profile", encoded)
            self.assertNotIn("password", encoded)
            if summary.get("read_state") == "AVAILABLE" and summary["attempt_count"]:
                self.assertEqual(summary["attempts"][0]["state"], "UNKNOWN")
                self.assertNotIn("error_code", summary["attempts"][0])

    def test_dead_letter_journal_keeps_safe_terminal_operation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            journal = ui_e2e.EvidenceJournal(
                target,
                {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "abc123"}},
            )
            journal.begin("Run dry-run")
            operation_id = "a" * 64
            with self.assertRaises(RuntimeError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda _path: {
                        "operation_id": operation_id,
                        "state": "DEAD_LETTERED",
                        "error_code": "APPLICATION_REVIEW_REQUIRED",
                    },
                    "/mock-runs/request-1",
                    operation_id,
                    failure_context={
                        "application_id": "c" * 64,
                        "request_id": "synthetic-request-1",
                        "expected_profile_version": "d" * 64,
                        "expected_document_version": 7,
                        "application_state": "PREPARED",
                        "preflight_result": "PASS",
                        "preflight_is_current": False,
                        "submit": False,
                        "profile_text": "must never be included",
                    },
                )

            journal.fail(caught.exception)
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["overall_state"], "FAIL")
            self.assertEqual(saved["remaining_state"], "NOT_RUN")
            self.assertEqual(saved["checks"], [
                {"action": "Run dry-run", "result": "FAIL"},
            ])
            self.assertEqual(saved["failure"]["terminal_operation"], {
                "kind": "APPLY_MOCK",
                "operation_id": operation_id,
                "application_id": "c" * 64,
                "request_id": "synthetic-request-1",
                "state": "DEAD_LETTERED",
                "error_code": "APPLICATION_REVIEW_REQUIRED",
                "error_present": True,
                "error_redacted": False,
                "expected_profile_version": "d" * 64,
                "expected_document_version": 7,
                "application_state": "PREPARED",
                "preflight_result": "PASS",
                "preflight_is_current": False,
                "submit": False,
            })
            self.assertNotIn("must never be included", target.read_text(encoding="utf-8"))

    def test_camel_case_builtin_error_is_saved_as_category_not_raw_type(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            journal = ui_e2e.EvidenceJournal(
                target,
                {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "abc123"}},
            )
            journal.begin("Submit to local mock")
            operation_id = "e" * 64
            with self.assertRaises(RuntimeError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda _path: {
                        "operation_id": operation_id,
                        "state": "DEAD_LETTERED",
                        "error_code": "TypeError",
                    },
                    "/mock-runs/synthetic-request",
                    operation_id,
                    failure_context={
                        "application_id": "f" * 64,
                        "request_id": "synthetic-request",
                        "submit": True,
                    },
                )

            journal.fail(caught.exception)
            encoded = target.read_text(encoding="utf-8")
            terminal = json.loads(encoded)["failure"]["terminal_operation"]
            self.assertEqual(terminal["error_category"], "BUILTIN_TYPE_ERROR")
            self.assertTrue(terminal["error_present"])
            self.assertTrue(terminal["error_redacted"])
            self.assertNotIn("error_code", terminal)
            self.assertNotIn("TypeError", encoded)

    def test_known_internal_error_type_is_saved_as_allowlisted_category(self):
        operation_id = "1" * 64
        with self.assertRaises(ui_e2e.MockRunTerminalError) as caught:
            ui_e2e.wait_for_mock_run(
                lambda _path: {
                    "operation_id": operation_id,
                    "state": "DEAD_LETTERED",
                    "error_code": "OperationTimedOut",
                },
                "/mock-runs/synthetic-request",
                operation_id,
            )
        summary = caught.exception.terminal_summary
        self.assertEqual(summary["error_category"], "INTERNAL_OPERATION_TIMEOUT")
        self.assertTrue(summary["error_present"])
        self.assertTrue(summary["error_redacted"])
        self.assertNotIn("error_code", summary)
        self.assertNotIn("OperationTimedOut", json.dumps(summary))

    def test_dead_letter_journal_drops_untrusted_error_code_text(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            journal = ui_e2e.EvidenceJournal(
                target,
                {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "abc123"}},
            )
            journal.begin("Run dry-run")
            operation_id = "b" * 64
            with self.assertRaises(RuntimeError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda _path: {
                        "operation_id": operation_id,
                        "state": "DEAD_LETTERED",
                        "error_code": "private synthetic payload",
                    },
                    "/mock-runs/request-2",
                    operation_id,
                    failure_context={
                        "application_id": "not-a-synthetic-id",
                        "request_id": "private synthetic payload",
                        "expected_profile_version": "untrusted version",
                        "expected_document_version": "7",
                        "application_state": "private synthetic payload",
                        "preflight_result": "private synthetic payload",
                        "preflight_is_current": "yes",
                        "submit": "true",
                    },
                )

            journal.fail(caught.exception)
            encoded = target.read_text(encoding="utf-8")
            saved = json.loads(encoded)
            terminal = saved["failure"]["terminal_operation"]
            self.assertTrue(terminal["error_present"])
            self.assertTrue(terminal["error_redacted"])
            self.assertNotIn("error_code", terminal)
            self.assertNotIn("error_category", terminal)
            self.assertNotIn("private synthetic payload", encoded)
            self.assertEqual(saved["failure"]["terminal_operation"], {
                "kind": "APPLY_MOCK",
                "state": "DEAD_LETTERED",
                "operation_id": operation_id,
                "error_present": True,
                "error_redacted": True,
            })

    def test_partial_evidence_keeps_completed_checks_and_sanitizes_injected_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            journal = ui_e2e.EvidenceJournal(
                target,
                {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "abc123"}},
            )
            journal.begin("Save synthetic profile")
            journal.complete({"action": "Synthetic profile saved", "result": "PASS"})

            journal.begin("Save selected Sources search")
            with self.assertRaisesRegex(ValueError, "private synthetic payload"):
                try:
                    raise ValueError("private synthetic payload")
                except ValueError as error:
                    journal.fail(error)
                    raise

            evidence = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(evidence["overall_state"], "FAIL")
            self.assertEqual(evidence["checks"], [
                {"action": "Synthetic profile saved", "result": "PASS"},
                {"action": "Save selected Sources search", "result": "FAIL"},
            ])
            self.assertEqual(evidence["failure"], {
                "type": "ValueError", "stage": "Save selected Sources search"})
            self.assertEqual(evidence["remaining_state"], "NOT_RUN")
            serialized = target.read_text(encoding="utf-8")
            self.assertNotIn("private synthetic payload", serialized)
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_default_demo_does_not_require_onboarding_blocked_operation(self):
        def unexpected_request(path):
            self.fail(f"demo run requested onboarding-only operation history: {path}")

        ui_e2e.assert_previous_review_operation(
            unexpected_request, onboarding=False, operation_id=None, application_id=None)

    def test_onboarding_recovery_requires_prior_operation_to_remain_blocked(self):
        calls = []

        def request(path):
            calls.append(path)
            return {"kind": "APPLY_MOCK", "entity_id": "application-1", "state": "BLOCKED"}

        ui_e2e.assert_previous_review_operation(
            request, onboarding=True, operation_id="prior-operation", application_id="application-1")
        self.assertEqual(calls, ["/operations/prior-operation"])

        with self.assertRaisesRegex(AssertionError, "prior blocked operation"):
            ui_e2e.assert_previous_review_operation(
                request, onboarding=True, operation_id=None, application_id="application-1")

        for invalid in (
            {"kind": "DISCOVER", "entity_id": "application-1", "state": "BLOCKED"},
            {"kind": "APPLY_MOCK", "entity_id": "another-application", "state": "BLOCKED"},
            {"kind": "APPLY_MOCK", "entity_id": "application-1", "state": "COMPLETED"},
            {"operation_type": "APPLY_MOCK", "entity_id": "application-1", "state": "BLOCKED"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                ui_e2e.assert_previous_review_operation(
                    lambda _path, record=invalid: record,
                    onboarding=True, operation_id="prior-operation", application_id="application-1")

    def test_fixture_discovery_accepts_only_supported_sources_tab_search_contract(self):
        search = {
            "id": "data-search", "profile_id": "synthetic-profile", "enabled": True,
            "source_ids": [],
            "criteria": {
                "include_titles": ["Data Engineer"], "remote": "REQUIRED",
                "employment_types": [],
                "scheduler": {"enabled": False, "interval_seconds": 3600},
            },
        }
        ui_e2e.assert_supported_fixture_search(
            search, profile_id="synthetic-profile", include_titles=["Data Engineer"],
            remote="REQUIRED",
        )

        for invalid in (
            {**search, "profile_id": "another-profile"},
            {**search, "enabled": False},
            {**search, "criteria": {**search["criteria"], "include_titles": ["IT Support"]}},
            {**search, "criteria": {**search["criteria"], "job_filters": {"q": "data"}}},
            {**search, "source_ids": ["employer-board"]},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AssertionError):
                    ui_e2e.assert_supported_fixture_search(
                        invalid, profile_id="synthetic-profile", include_titles=["Data Engineer"],
                        remote="REQUIRED",
                    )

    def test_reprepare_profile_pins_use_opaque_hash_equality_not_order(self):
        previous = "f" * 64
        current = "0" * 64
        ui_e2e.assert_reprepared_profile_pins(
            previous_profile_version=previous,
            old_application_profile_version=previous,
            current_profile_version=current,
            resumed_application_profile_version=current,
        )
        with self.assertRaises(AssertionError):
            ui_e2e.assert_reprepared_profile_pins(
                previous_profile_version=previous,
                old_application_profile_version="e" * 64,
                current_profile_version=current,
                resumed_application_profile_version=current,
            )
        with self.assertRaises(AssertionError):
            ui_e2e.assert_reprepared_profile_pins(
                previous_profile_version=previous,
                old_application_profile_version=previous,
                current_profile_version=current,
                resumed_application_profile_version=previous,
            )

    def test_onboarding_write_firewall_allows_only_local_prepare_reprepare_and_mock_run(self):
        allowed = (
            ("POST", "/applications"),
            ("POST", "/profiles/p1/applications/a1/reprepare"),
            ("POST", "/profiles/p1/applications/a1/mock-run"),
            ("POST", "/profiles/p1/applications/a1/uploaded-resume-comparison/operations"),
            ("POST", "/profiles/p1/applications/a1/uploaded-resume-comparison/operations/op1/cancel"),
        )
        blocked = (
            ("PUT", "/profiles/p1/applications/a1/reprepare"),
            ("GET", "/profiles/p1/applications/a1/uploaded-resume-comparison/operations"),
            ("POST", "/profiles/p1/applications/a1/uploaded-resume-comparison/operations/op1/cancel/extra"),
            ("POST", "/profiles/p1/applications/a1/submit"),
            ("POST", "/profiles/p1/applications/a1/mock-run/extra"),
            ("POST", "/profiles/p1/applications/a1/external-application"),
        )
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                self.assertTrue(ui_e2e.is_allowed_onboarding_application_write(method, path))
        for method, path in blocked:
            with self.subTest(method=method, path=path):
                self.assertFalse(ui_e2e.is_allowed_onboarding_application_write(method, path))

    def test_onboarding_request_firewall_keeps_comparison_local_and_submission_blocked(self):
        origin = "127.0.0.1:18700"
        comparison = "http://127.0.0.1:18700/profiles/private-profile/applications/private-app/" \
                    "uploaded-resume-comparison/operations?request_id=private-query"
        self.assertTrue(ui_e2e.is_allowed_onboarding_request("POST", comparison, origin))
        self.assertFalse(ui_e2e.is_allowed_onboarding_request(
            "POST", comparison, "employer.example"))
        self.assertFalse(ui_e2e.is_allowed_onboarding_request(
            "POST", "http://127.0.0.1:18700/profiles/private-profile/applications/private-app/submit", origin))
        self.assertFalse(ui_e2e.is_allowed_onboarding_request(
            "POST", "http://127.0.0.1:18700/apply", origin))
        self.assertEqual(ui_e2e.sanitized_onboarding_request("POST", comparison), {
            "method": "POST",
            "path": "/profiles/<profile>/applications/<application>/uploaded-resume-comparison/operations",
        })

    def test_discovery_oracle_requires_selected_search_and_exact_filtered_readback(self):
        ui_e2e.assert_discovery_request_operation_pair(
            {"request_id": "matching-request", "profile_id": "synthetic-profile",
             "search_profile_id": "matching-search"},
            {"id": "matching-operation", "entity_id": "synthetic-profile",
             "params": {"request_id": "matching-request",
                        "search_profile_id": "matching-search"}},
            profile_id="synthetic-profile", search_profile_id="matching-search",
            expected_operation_id="matching-operation",
        )
        with self.assertRaisesRegex(AssertionError, "request.*operation"):
            ui_e2e.assert_discovery_request_operation_pair(
                {"request_id": "nonmatching-request", "profile_id": "synthetic-profile",
                 "search_profile_id": "matching-search"},
                {"id": "matching-operation", "entity_id": "synthetic-profile",
                 "params": {"request_id": "matching-request",
                            "search_profile_id": "matching-search"}},
                profile_id="synthetic-profile", search_profile_id="matching-search",
                expected_operation_id="matching-operation",
            )
        with self.assertRaisesRegex(AssertionError, "request.*operation"):
            ui_e2e.assert_discovery_request_operation_pair(
                {"request_id": "matching-request", "profile_id": "synthetic-profile",
                 "search_profile_id": "matching-search"},
                {"id": "older-operation", "entity_id": "synthetic-profile",
                 "params": {"request_id": "matching-request",
                            "search_profile_id": "matching-search"}},
                profile_id="synthetic-profile", search_profile_id="matching-search",
                expected_operation_id="matching-operation",
            )
        request_payload = {
            "profile_id": "synthetic-profile",
            "search_profile_id": "matching-search",
        }
        operation = {
            "entity_id": "synthetic-profile",
            "params": {
                "profile_id": "synthetic-profile",
                "search_profile_id": "matching-search",
            },
        }

        ui_e2e.assert_search_scoped_discovery(
            request_payload, operation,
            [{"id": "data-job", "title": "Data Engineer", "profile_id": "synthetic-profile"}],
            profile_id="synthetic-profile", search_profile_id="matching-search",
            expected_titles={"Data Engineer"},
        )
        ui_e2e.assert_search_scoped_discovery(
            request_payload, operation, [],
            profile_id="synthetic-profile", search_profile_id="matching-search",
            expected_titles=set(),
        )

        for bad_payload, bad_operation, rows in (
            ({**request_payload, "search_profile_id": None}, operation, []),
            (request_payload, {**operation, "entity_id": "another-profile"}, []),
            (request_payload, operation, [
                {"id": "python-job", "title": "Python Developer", "profile_id": "synthetic-profile"},
            ]),
        ):
            with self.subTest(payload=bad_payload, operation=bad_operation, rows=rows):
                with self.assertRaises(AssertionError):
                    ui_e2e.assert_search_scoped_discovery(
                        bad_payload, bad_operation, rows,
                        profile_id="synthetic-profile", search_profile_id="matching-search",
                        expected_titles={"Data Engineer"},
                    )

    def test_preflight_rejects_nonfixture_external_or_degraded_runtime(self):
        settings = {"operation_mode": "FIXTURE", "external_submission_enabled": False}
        health = {"operation_mode": "FIXTURE", "external_submission_enabled": False}
        readiness = {"status": "ready"}
        ui_e2e.validate_onboarding_preflight(settings, health, readiness)

        for unsafe_settings, unsafe_health, unsafe_readiness in (
            ({**settings, "operation_mode": "PERSONAL"}, health, readiness),
            (settings, {**health, "external_submission_enabled": True}, readiness),
            ({**settings, "external_submission_enabled": "false"}, health, readiness),
            (settings, health, {"status": "degraded"}),
        ):
            with self.subTest(settings=unsafe_settings, health=unsafe_health,
                              readiness=unsafe_readiness):
                with self.assertRaisesRegex(RuntimeError, "onboarding requires ready FIXTURE"):
                    ui_e2e.validate_onboarding_preflight(unsafe_settings, unsafe_health,
                                                         unsafe_readiness)

    def test_onboarding_target_is_http_loopback_only(self):
        for url in ("http://127.0.0.1:18810", "http://localhost:18700", "http://[::1]:18810"):
            with self.subTest(url=url):
                ui_e2e.validate_onboarding_target(url)
        for url in ("https://127.0.0.1:18810", "http://example.com:18810",
                    "http://127.0.0.1:18810@evil.example"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(RuntimeError, "HTTP loopback Compose service"):
                    ui_e2e.validate_onboarding_target(url)

    def test_failed_preflight_stops_before_output_creation_or_browser_launch(self):
        with patch.object(ui_e2e, "_onboarding_preflight",
                          side_effect=RuntimeError("unsafe fixture mode")) as preflight, \
                patch.object(Path, "mkdir", side_effect=AssertionError("output created")), \
                patch.object(ui_e2e, "sync_playwright",
                             side_effect=AssertionError("browser launched")):
            with self.assertRaisesRegex(RuntimeError, "unsafe fixture mode"):
                ui_e2e.run("http://127.0.0.1:18810", "unused-evidence.json", onboarding=True)
        preflight.assert_called_once_with("http://127.0.0.1:18810")

    def test_profile_readback_rejects_unsupported_approved_facts(self):
        profile = {"facts": [
            {"fact_id": "python-entry", "kind": "skill", "text": "Python", "verified": True,
             "user_approved": True},
            {"fact_id": "sql-entry", "kind": "skill", "text": "SQL", "verified": True,
             "user_approved": True},
            {"fact_id": "imported-summary", "kind": "summary", "text": "Synthetic imported summary",
             "verified": False, "user_approved": False},
        ]}
        ui_e2e.assert_onboarding_profile_facts(profile, {"imported-summary"})

        corrupted = {"facts": [*profile["facts"], {
            "fact_id": "invented-experience", "kind": "experience", "text": "Managed a production team",
            "verified": True, "user_approved": True,
        }]}
        with self.assertRaisesRegex(AssertionError, "unexpected approved facts"):
            ui_e2e.assert_onboarding_profile_facts(corrupted, {"imported-summary"})

    def test_documents_must_exclude_imported_pending_facts(self):
        ui_e2e.assert_no_pending_facts_in_documents([
            {"fact_ids": ["python-entry"], "facts_used": [
                {"fact_id": "python-entry", "verified": True, "user_approved": True},
            ]},
        ], {"imported-summary"}, {"python-entry", "sql-entry"})
        with self.assertRaisesRegex(AssertionError, "pending or unsupported facts reached generated documents"):
            ui_e2e.assert_no_pending_facts_in_documents([
                {"fact_ids": ["imported-summary"], "facts_used": [
                    {"fact_id": "imported-summary", "verified": False,
                     "user_approved": False},
                ]},
            ], {"imported-summary"}, {"python-entry", "sql-entry"})
        with self.assertRaisesRegex(AssertionError, "pending or unsupported facts reached generated documents"):
            ui_e2e.assert_no_pending_facts_in_documents([
                {"fact_ids": ["unsupported-skill"], "facts_used": [
                    {"fact_id": "unsupported-skill", "verified": True,
                     "user_approved": True},
                ]},
            ], {"imported-summary"}, {"python-entry", "sql-entry"})


if __name__ == "__main__":
    unittest.main()
