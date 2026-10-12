"""Real API/worker acceptance for explicitly local ATS fixture preparation."""
import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from tests.integration import test_application_form_review_flow as base_flow
from workers.tasks import execute_operation
from services.applications import controlled_dry_run as service


class ControlledApplicationDryRunTests(unittest.TestCase):
    _new_store = base_flow.ApplicationFormReviewFlowTests._new_store
    def setUp(self):
        from tests.integration.test_spine import PROFILE
        import copy
        profile = copy.deepcopy(PROFILE)
        profile["answers"].append({"id": "weekends", "question": "Can you work weekends?",
                                   "text": "Yes, weekends are available.",
                                   "verified": True, "user_approved": True, "confidence": 100,
                                   "source": "fictional-test-profile"})
        with patch("tests.integration.test_application_form_review_flow.PROFILE", profile):
            base_flow.ApplicationFormReviewFlowTests.setUp(self)

    tearDown = base_flow.ApplicationFormReviewFlowTests.tearDown

    def path(self):
        return f"/profiles/fixture/applications/{self.application_id}/controlled-dry-run"

    def execute(self, operation_id):
        with patch.dict("os.environ", {
            "MJOBS_OPERATION_MODE": "FIXTURE", "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root),
        }):
            return execute_operation(operation_id)

    def review_and_queue(self, request_id="controlled-run"):
        response = self.client.get(self.path())
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertTrue(preview["ready"], preview)
        self.assertFalse(preview["submission_enabled"])
        self.assertEqual(preview["context"], "CONTROLLED_FIXTURE")
        reviewed = self.client.post(self.path()+"/reviews", json={
            "request_id": request_id+"-review", "fingerprint": preview["fingerprint"],
            "acknowledged": True,
        })
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        body = {"request_id": request_id, "fingerprint": preview["fingerprint"]}
        queued = self.client.post(self.path()+"/runs", json=body)
        self.assertEqual(queued.status_code, 202, queued.text)
        return preview, body, queued.json()

    def test_both_providers_review_run_reload_and_replay_without_submission(self):
        for provider, url in (
            ("greenhouse", "https://boards.greenhouse.io/example/jobs/12345"),
            ("lever", "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef"),
        ):
            with self.subTest(provider=provider):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.store.application(self.application_id)["route_id"],
                    ).values(provider=provider, url=url))
                before = self.store.application(self.application_id)
                preview, body, queued = self.review_and_queue(provider)
                result = self.execute(queued["operation_id"])
                self.assertEqual(result["status"], "COMPLETED", result)
                receipt = self.client.get(self.path()+"/runs/"+body["request_id"]).json()
                self.assertEqual(receipt["state"], "COMPLETED", receipt)
                self.assertEqual(receipt["receipt"]["verification"], "BROWSER_SELECTED_BYTES")
                self.assertFalse(receipt["receipt"]["provider_acceptance_claimed"])
                self.assertEqual(len(receipt["receipt"]["documents"]), 2)
                retry = self.client.post(self.path()+"/runs", json=body)
                self.assertEqual(retry.status_code, 202, retry.text)
                self.assertEqual(retry.json()["operation_id"], queued["operation_id"])
                self.assertEqual(self.execute(queued["operation_id"])["status"], "ALREADY_CLAIMED_OR_CAPACITY")
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == queued["operation_id"])
                                       .values(state="QUEUED"))
                with patch("services.applications.controlled_dry_run.run_browser",
                           side_effect=AssertionError("completed receipt must avoid replay")):
                    self.assertEqual(self.execute(queued["operation_id"])["status"], "COMPLETED")
                self.assertEqual(self.client.get(self.path()+"/runs/"+body["request_id"]).json(), receipt)
                self.assertEqual(self.store.application(self.application_id), before)
                self.assertEqual(self.store.count("application_attempts"), 0)
                self.assertNotIn(preview["fields"][0]["value"], json.dumps(receipt))

    def test_unreviewed_and_stale_inputs_do_not_admit_or_complete(self):
        preview = self.client.get(self.path())
        self.assertEqual(preview.status_code, 200, preview.text)
        denied = self.client.post(self.path()+"/runs", json={
            "request_id": "unreviewed", "fingerprint": preview.json()["fingerprint"],
        })
        self.assertEqual(denied.status_code, 409)
        _, body, queued = self.review_and_queue()
        profile = self.store.profile("fixture")
        profile["name"] = "Changed after review"
        self.store.save_profile(profile, expected_version=profile["version"])
        self.assertEqual(self.client.post(self.path()+"/runs", json=body).status_code, 409)
        result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        receipt = self.client.get(self.path()+"/runs/"+body["request_id"]).json()
        self.assertIsNone(receipt["receipt"])

    def test_cross_profile_and_untrusted_fields_are_rejected(self):
        self.assertEqual(self.client.get(self.path().replace("/fixture/", "/other/")).status_code, 404)
        rejected = self.client.post(self.path()+"/runs", json={
            "request_id": "unsafe", "fingerprint": "a"*64,
            "url": "https://attacker.invalid", "submit": True,
        })
        self.assertEqual(rejected.status_code, 422)

    def test_selected_supporting_bytes_are_verified_and_public_selection_stays_closed(self):
        upload = self.client.post("/profiles/fixture/document-library", content=b"Synthetic supporting evidence",
            headers={"Content-Type": "text/plain", "X-Filename": "evidence.txt",
                     "X-Document-Kind": "SUPPORTING_DOCUMENT", "X-Document-Label": "Fixture evidence"})
        self.assertEqual(upload.status_code, 201, upload.text)
        revision = upload.json()["revision_id"]
        selection = {"request_id": "fixture-supporting", "revision_ids": [revision]}
        public = self.path().replace("/controlled-dry-run", "/supporting-documents")
        public_before = self.client.get(public).json()
        self.assertEqual(self.client.put(public, json=selection).status_code, 422)
        selected = self.client.put(self.path()+"/supporting-documents", json=selection)
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(self.client.get(public).json(), public_before)
        _, _, queued = self.review_and_queue()
        self.assertEqual(self.execute(queued["operation_id"])["status"], "COMPLETED")
        receipt = self.client.get(self.path()+"/runs/controlled-run").json()["receipt"]
        supporting = [item for item in receipt["documents"] if item["kind"] == "supporting-document"]
        self.assertEqual(supporting[0]["document_id"], revision)
        import hashlib
        self.assertEqual(supporting[0]["sha256"], hashlib.sha256(b"Synthetic supporting evidence").hexdigest())

    def test_document_bytes_changed_after_review_block_before_browser(self):
        _, _, queued = self.review_and_queue()
        with self.store.engine.connect() as connection:
            document = connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id, m.documents.c.kind == "resume",
            )).mappings().first()
        (self.root / self.application_id / document["files"]["pdf"]).write_bytes(b"%PDF-corrupted")
        with patch.object(service, "run_browser", side_effect=AssertionError("must not launch")):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        read = self.client.get(self.path()+"/runs/controlled-run").json()
        self.assertEqual(read["error_code"], "CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
        self.assertIsNone(read["receipt"])

    def test_template_change_invalidates_acknowledgement_before_browser(self):
        _, _, queued = self.review_and_queue()
        original = service.template
        def changed(provider):
            fixture = original(provider)
            fixture["template_sha256"] = "f"*64
            return fixture
        with patch.object(service,"template",changed), patch.object(service,"run_browser",
                side_effect=AssertionError("must not launch")):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)

    def test_unverified_answer_blocks_admission_and_cancellation_saves_no_receipt(self):
        _, _, queued = self.review_and_queue()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id==queued["operation_id"])
                               .values(cancel_requested=True))
        with patch.object(service,"run_browser",side_effect=AssertionError("cancelled work must not launch")):
            result = self.execute(queued["operation_id"])
        self.assertNotEqual(result["status"], "COMPLETED", result)
        self.assertIsNone(self.client.get(self.path()+"/runs/controlled-run").json()["receipt"])
        profile = self.store.profile("fixture")
        profile["answers"][-1]["user_approved"] = False
        self.store.save_profile(profile, expected_version=profile["version"])
        preview = self.client.get(self.path()).json()
        self.assertFalse(preview["ready"])
        self.assertEqual(self.client.post(self.path()+"/runs", json={
            "request_id": "unapproved", "fingerprint": preview["fingerprint"],
        }).status_code, 409)

    def test_ambiguous_fixture_controls_block_before_any_document_selection(self):
        import hashlib
        original = service.template
        def duplicate(provider):
            fixture = original(provider)
            fixture["html"] = fixture["html"].replace("</form>", '<input name="email"></form>')
            fixture["template_sha256"] = hashlib.sha256(fixture["html"].encode()).hexdigest()
            return fixture
        with patch.object(service, "template", duplicate):
            _, _, queued = self.review_and_queue()
            with patch.object(service, "attach_verified_files", side_effect=AssertionError("ambiguous form must not upload")):
                result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertIsNone(self.client.get(self.path()+"/runs/controlled-run").json()["receipt"])

    def test_profile_change_during_browser_prevents_receipt_commit(self):
        _, _, queued = self.review_and_queue()
        original = service.run_browser
        def changed(*args, **kwargs):
            manifest = original(*args, **kwargs)
            profile = self.store.profile("fixture")
            profile["name"] = "Changed during controlled browser"
            self.store.save_profile(profile, expected_version=profile["version"])
            return manifest
        with patch.object(service, "run_browser", changed):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertIsNone(self.client.get(self.path()+"/runs/controlled-run").json()["receipt"])

    def test_stale_worker_attempt_cannot_commit_receipt(self):
        _, _, queued = self.review_and_queue()
        original = service.run_browser
        def replaced(*args, **kwargs):
            manifest = original(*args, **kwargs)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id==queued["operation_id"])
                                   .values(attempt_count=99))
            return manifest
        with patch.object(service, "run_browser", replaced):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "OWNERSHIP_EXPIRED", result)
        self.assertIsNone(self.client.get(self.path()+"/runs/controlled-run").json()["receipt"])

    def test_prepared_path_escape_is_rejected_before_browser(self):
        _, _, queued = self.review_and_queue()
        with self.store.engine.begin() as connection:
            document = connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id, m.documents.c.kind == "resume",
            )).mappings().first()
            files = {**document["files"], "pdf": "../outside.pdf"}
            connection.execute(update(m.documents).where(m.documents.c.id==document["id"]).values(files=files))
        with patch.object(service,"run_browser",side_effect=AssertionError("must not launch")):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertIsNone(self.client.get(self.path()+"/runs/controlled-run").json()["receipt"])

    def test_file_readback_obeys_remaining_operation_budget(self):
        import time
        from workers.operations import OperationTimedOut
        current = service.preview(self.store,"fixture",self.application_id)
        prepared,supporting=service._snapshots(self.store,self.root,current)
        class Budget:
            deadline=time.monotonic()+15
            def __call__(self):
                if self.remaining_seconds()<=0:raise OperationTimedOut("Synthetic deadline")
            def remaining_seconds(self):return self.deadline-time.monotonic()
        budget=Budget()
        original=service.attach_verified_files
        started=[]
        def delayed(locator,snapshots,**kwargs):
            locator.page.evaluate("FileReader.prototype.readAsDataURL = function() {}")
            started.append(time.monotonic())
            budget.deadline=time.monotonic()+.2
            return original(locator,snapshots,**kwargs)
        with patch.object(service,"attach_verified_files",delayed):
            with self.assertRaises(OperationTimedOut):
                service.run_browser(current,prepared,supporting,check=budget)
        self.assertLess(time.monotonic()-started[0],2,"File readback ignored the operation budget")

    def test_file_locator_resolution_uses_fresh_remaining_budget(self):
        import time
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        from services.applications.upload_integrity import verify_file_selection
        current=service.preview(self.store,"fixture",self.application_id)
        prepared,_=service._snapshots(self.store,self.root,current)
        class Budget:
            def __call__(self):pass
            def remaining_seconds(self):return .2
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                page.set_default_timeout(5000)
                page.set_content("<form></form>")
                started=time.monotonic()
                with self.assertRaises(BrowserTimeout):
                    verify_file_selection(page.locator('input[type="file"]'),[prepared[0]],check=Budget())
                self.assertLess(time.monotonic()-started,2,"Locator wait used an older page timeout")
            finally:browser.close()


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database")
class ControlledApplicationDryRunPostgresTests(ControlledApplicationDryRunTests):
    from tests.integration.test_lever_form_review_flow import LeverFormReviewPostgresFlowTests as _Fixture
    _new_store = _Fixture._new_store
    _drop_schema = _Fixture._drop_schema


if __name__ == "__main__":
    unittest.main()
