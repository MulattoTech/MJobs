import unittest
from pathlib import Path
from unittest.mock import patch

from cli import ui_e2e


class UIE2EOnboardingGuardTests(unittest.TestCase):
    def test_discovery_oracle_requires_selected_search_and_exact_filtered_readback(self):
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
