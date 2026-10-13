"""Acceptance for ordered, two-page controlled application dry runs."""
import json
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import update

from packages.persistence import models as m
from packages.persistence.store import identity
from apps.api.main import create_app
from tests.integration import test_controlled_application_dry_run as base
from services.applications import controlled_dry_run as service


CONTACT_PAGE = {
    "id": "contact",
    "title": "Contact and answer",
    "field_keys": ["full_name", "email", "weekend_availability"],
}
DOCUMENTS_PAGE = {
    "id": "documents",
    "title": "Documents",
    "field_keys": ["resume", "cover_letter", "supporting_documents"],
}
EXPECTED_PAGES = [CONTACT_PAGE, DOCUMENTS_PAGE]


class ControlledMultipageDryRunTests(unittest.TestCase):
    """Reuse the connected SQLite/API/worker fixture without inheriting tests."""

    _new_store = base.ControlledApplicationDryRunTests._new_store
    setUp = base.ControlledApplicationDryRunTests.setUp
    tearDown = base.ControlledApplicationDryRunTests.tearDown
    path = base.ControlledApplicationDryRunTests.path
    execute = base.ControlledApplicationDryRunTests.execute
    review_and_queue = base.ControlledApplicationDryRunTests.review_and_queue

    def _select_provider(self, provider, url):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(provider=provider, url=url))

    def _assert_provider_success(self, provider, url):
        self._select_provider(provider, url)
        preview, body, queued = self.review_and_queue("multipage-" + provider)
        self.assertEqual(preview["pages"], EXPECTED_PAGES)
        self.assertEqual(self.execute(queued["operation_id"])["status"], "COMPLETED")
        self.store.engine.dispose()
        self.store = self._new_store()
        self.client.close()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

        receipt_response = self.client.get(self.path() + "/runs/" + body["request_id"])
        self.assertEqual(receipt_response.status_code, 200, receipt_response.text)
        receipt = receipt_response.json()["receipt"]
        self.assertEqual(receipt["pages"], [
            {"id": "contact", "control_count": 3, "validation": "PASS"},
            {"id": "documents", "control_count": 3, "validation": "PASS"},
        ])
        self.assertEqual(len(receipt["documents"]), 2)
        self.assertFalse(receipt["provider_acceptance_claimed"])
        self.assertFalse(receipt["submission_enabled"])
        serialized = json.dumps(receipt)
        for field in preview["fields"]:
            if field.get("value"):
                self.assertNotIn(field["value"], serialized)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_greenhouse_completes_both_pages_and_reads_back_value_free_receipt(self):
        self._assert_provider_success("greenhouse", "https://boards.greenhouse.io/example/jobs/12345")

    def test_lever_completes_both_pages_and_reads_back_value_free_receipt(self):
        self._assert_provider_success("lever", "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef")

    def _assert_tampered_second_page_fails(self, case, mutate):
        original = service.template
        def tampered(provider):
            fixture = original(provider)
            fixture.setdefault("document_html", """<form data-controlled-page="documents">
                <input name="resume" type="file" required>
                <input name="cover_letter" type="file" required>
                <input name="supporting_documents" type="file" multiple>
                </form>""")
            fixture["document_html"] = mutate(fixture["document_html"])
            # The patched template is stable across preview and execution; the
            # digest is only a fixture identity here, not the behavior under test.
            fixture["template_sha256"] = "d" * 64
            return fixture

        with patch.object(service, "template", tampered):
            _, body, queued = self.review_and_queue("tampered-" + case)
            browser = Mock(wraps=service.run_browser)
            with patch.object(service, "run_browser", browser):
                result = self.execute(queued["operation_id"])
            self.assertEqual(result["status"], "BLOCKED", result)
            browser.assert_called_once()
            saved = self.client.get(self.path() + "/runs/" + body["request_id"])
            self.assertIsNone(saved.json()["receipt"])
            self.assertEqual(saved.json()["error_code"], "CONTROLLED_DRY_RUN_REVIEW_REQUIRED")

    def test_duplicate_control_on_second_page_fails_without_receipt(self):
        self._assert_tampered_second_page_fails("duplicate-control", lambda html: html.replace(
            "</form>", '<input name="email"></form>', 1))

    def test_wrong_second_page_identity_fails_without_receipt(self):
        self._assert_tampered_second_page_fails("wrong-identity", lambda html: html.replace(
            'data-controlled-page="documents"', 'data-controlled-page="contact"', 1))

    def test_second_page_template_change_after_review_blocks_before_browser(self):
        _, body, queued = self.review_and_queue("second-page-changed")
        original = service.template

        def changed(provider):
            fixture = original(provider)
            fixture.setdefault("document_html", "<form data-controlled-page='documents'></form>")
            fixture["document_html"] = fixture["document_html"].replace(
                "supporting_documents", "tampered_supporting_documents", 1)
            fixture["template_sha256"] = "f" * 64
            return fixture

        browser = Mock(wraps=service.run_browser)
        with patch.object(service, "template", changed), patch.object(service, "run_browser", browser):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        browser.assert_not_called()
        saved = self.client.get(self.path() + "/runs/" + body["request_id"])
        self.assertIsNone(saved.json()["receipt"])
        self.assertEqual(saved.json()["error_code"], "CONTROLLED_DRY_RUN_STALE")

    def test_page_plan_change_after_review_blocks_before_browser(self):
        _, body, queued = self.review_and_queue("page-plan-changed")
        original = service.template

        def reordered(provider):
            fixture = original(provider)
            fixture.setdefault("pages", EXPECTED_PAGES)
            fixture["pages"] = list(reversed(fixture["pages"]))
            fixture["template_sha256"] = "e" * 64
            return fixture

        browser = Mock(wraps=service.run_browser)
        with patch.object(service, "template", reordered), patch.object(service, "run_browser", browser):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        browser.assert_not_called()
        saved = self.client.get(self.path() + "/runs/" + body["request_id"])
        self.assertIsNone(saved.json()["receipt"])
        self.assertEqual(saved.json()["error_code"], "CONTROLLED_DRY_RUN_STALE")

    def test_legacy_receipt_without_pages_remains_readable_unchanged(self):
        _, body, queued = self.review_and_queue("legacy-receipt")
        legacy_receipt = {
            "operation_id": queued["operation_id"],
            "profile_id": "fixture",
            "application_id": self.application_id,
            "fingerprint": body["fingerprint"],
            "context": "CONTROLLED_FIXTURE",
            "template_id": "greenhouse-local-v1",
            "template_sha256": "a" * 64,
            "verification": "BROWSER_SELECTED_BYTES",
            "documents": [
                {"kind": "resume", "size": 4, "sha256": "b" * 64},
                {"kind": "cover-letter", "size": 5, "sha256": "c" * 64},
            ],
            "provider_acceptance_claimed": False,
            "submission_enabled": False,
        }
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id=identity(service.KIND, queued["operation_id"]),
                entity_id=self.application_id,
                action=service.KIND,
                timestamp="2026-10-13T00:00:00+00:00",
                payload=legacy_receipt,
            ))

        saved = self.client.get(self.path() + "/runs/" + body["request_id"])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["receipt"], legacy_receipt)
        self.assertNotIn("pages", saved.json()["receipt"])


if __name__ == "__main__":
    unittest.main()
