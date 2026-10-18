import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from cli import ui_e2e


class UIE2EOnboardingGuardTests(unittest.TestCase):
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
