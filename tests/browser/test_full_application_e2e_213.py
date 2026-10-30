"""Connected synthetic application journey for MJ-REQ-213."""
from copy import deepcopy

from playwright.sync_api import expect
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import read_attempt_history
from services.discovery.pipeline import discover
from tests.browser.test_mock_run_controls import MockRunBrowserHarness
from tests.integration.test_spine import PROFILE


class FullApplicationE2EBrowserTests(MockRunBrowserHarness):
    def seed_workflow(self):
        # A required fixture answer is deliberately withheld so the first
        # application attempt proves the review gate before the corrected run.
        profile = deepcopy(PROFILE)
        answer = next(row for row in profile["answers"]
                      if row["id"] == "fixture_question")
        answer["question"] = "Approved fixture answer"
        answer["verified"] = False
        answer["user_approved"] = False
        self.store.save_profile(profile)
        discover(self.store, self.fixture, "fixture")
        self.job = next(row for row in self.store.jobs("fixture")
                        if row["requisition"] == "B")
        self.app_id = None

    def open_initial_view(self):
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Pipeline", exact=True).click()
        self.workbench = self.page.get_by_role("region", name="Pipeline workbench")
        expect(self.workbench).to_contain_text(self.job["title"])

    def _api_json(self, path):
        response = self.context.request.get(self.origin + path)
        self.assertEqual(response.status, 200, response.text())
        return response.json()

    def client_application(self):
        return self._api_json(f"/profiles/fixture/applications/{self.app_id}")

    def client_documents(self):
        return self._api_json(
            f"/profiles/fixture/applications/{self.app_id}/documents")["documents"]

    def client_profile(self):
        return self._api_json("/profiles/fixture")

    def client_receipt(self, request_id):
        return self._api_json(
            f"/profiles/fixture/applications/{self.app_id}/mock-runs/{request_id}")

    def test_job_b_to_reviewed_single_mock_confirmation_survives_reopen(self):
        self.assertEqual(self.job["requisition"], "B")
        expect(self.page.get_by_text("External submission OFF", exact=True)).to_be_visible()

        # Scoring and document creation are queued through the public UI and
        # executed by the real worker entrypoint against the local fixture.
        with self.page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url == self.origin + "/matches") as scoring:
            self.workbench.get_by_role(
                "button", name="Refresh all scores for this profile", exact=True).click()
        self.assertEqual(scoring.value.status, 202, scoring.value.text())
        score_operation = scoring.value.json()["operation_id"]
        self.run_worker(score_operation, refresh_panel=False)
        expect(self.workbench).to_contain_text("Score refresh completed")
        scored = next(row for row in self.store.jobs("fixture")
                      if row["id"] == self.job["id"])
        self.assertGreaterEqual(scored["match_score"], 75)

        row = self.workbench.locator("li.fact").filter(
            has=self.page.get_by_role("heading", name=self.job["title"], exact=True))
        with self.page.expect_response(
                lambda response: response.request.method == "POST"
                and "/pipeline-workbench/" in response.url
                and response.url.endswith("/prepare")) as preparing:
            row.get_by_role(
                "button", name="Prepare application documents", exact=True).click()
        self.assertEqual(preparing.value.status, 202, preparing.value.text())
        self.run_worker(preparing.value.json()["operation_id"], refresh_panel=False)
        expect(row.get_by_role(
            "button", name="Open existing application", exact=True)).to_be_visible()
        with self.store.engine.connect() as connection:
            self.app_id = connection.execute(select(m.applications.c.id).where(
                m.applications.c.job_id == self.job["id"],
                m.applications.c.profile_id == "fixture")).scalar_one()

        documents = self.context.request.get(
            f"{self.origin}/profiles/fixture/applications/{self.app_id}/documents")
        self.assertEqual(documents.status, 200, documents.text())
        self.assertEqual({item["kind"] for item in documents.json()["documents"]},
                         {"resume", "cover-letter"})
        self.open_application()
        expect(self.panel.get_by_role(
            "button", name="Submit to local mock", exact=True)).to_be_disabled()
        consent = self.panel.get_by_label(
            "I approve one fictional mock submission", exact=True)
        expect(consent).not_to_be_checked()
        self.assertEqual(self.counts()["submit"], 0)

        # Missing approval on a required ATS question must stop before submit.
        self.panel.get_by_role("button", name="Run dry-run", exact=True).click()
        expect(self.panel).to_contain_text("QUEUED")
        blocked_request = self.posts[0]["request_id"]
        blocked_operation = self.operations()[0]["id"]
        self.open_application()
        expect(self.panel).to_contain_text("QUEUED")
        self.assertEqual(len(self.posts), 1, "Reload duplicated the queued request")
        self.run_worker(blocked_operation, expected_status="BLOCKED",
                        expected_receipt_state="BLOCKED")
        expect(self.page.get_by_role(
            "status", name="Application state", exact=True)).to_have_text("REVIEW_REQUIRED")
        blocked_app = self.client_application()
        self.assertEqual(blocked_app["question_preflight"]["result"], "UNKNOWN")
        self.assertEqual(self.client_receipt(blocked_request)["state"], "BLOCKED")
        self.assertEqual(self.counts()["submit"], 0)

        preflight = self.page.get_by_role(
            "region", name="ATS question preflight", exact=True)
        preflight.get_by_role("button", name="Review this question", exact=True).click()
        question = self.page.get_by_role(
            "region", name="Application answers", exact=True).get_by_role(
                "group", name="Question 1")
        question.get_by_role("textbox", name="Answer", exact=True).fill(
            "Controlled fictional test application.")
        question.get_by_label("I reviewed this exact answer for this job", exact=True).check()
        question.get_by_role("button", name="Save to Answer Bank", exact=True).click()
        confirmation = self.page.locator(
            '[aria-label="Answer Bank confirmation for question 1"]')
        confirmation.get_by_label(
            "I confirm this answer is accurate and may be reused", exact=True).check()
        with self.page.expect_response(
                lambda response: response.request.method == "PUT"
                and response.url == self.origin + "/profiles") as saved:
            confirmation.get_by_role(
                "button", name="Confirm and save to Answer Bank", exact=True).click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        saved_profile = self.client_profile()
        reviewed = next(item for item in saved_profile["answers"]
                        if item["text"] == "Controlled fictional test application.")
        self.assertTrue(reviewed["verified"] and reviewed["user_approved"])

        old_app = self.client_application()
        old_documents = self.client_documents()
        with self.page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/reprepare")) as resumed:
            self.page.get_by_role(
                "button", name="Resume and prepare current revision", exact=True).click()
        self.assertEqual(resumed.value.status, 200, resumed.value.text())
        self.assertEqual(resumed.value.json()["profile_version"], saved_profile["version"])
        self.assertEqual(resumed.value.json()["document_version"],
                         old_app["document_version"] + 1)
        self.assertGreaterEqual(len(self.client_documents()), len(old_documents) + 2)

        # Clear only the terminal browser intent, then validate the current
        # document/profile revision in a fresh dry-run before explicit consent.
        self.panel.get_by_role(
            "button", name="Start a new reviewed run", exact=True).click()
        expect(self.panel.get_by_role(
            "button", name="Run dry-run", exact=True)).to_be_enabled()
        expect(self.panel.get_by_role(
            "button", name="Submit to local mock", exact=True)).to_be_disabled()
        with self.page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/mock-run")) as dry_run:
            self.panel.get_by_role("button", name="Run dry-run", exact=True).click()
        self.assertEqual(dry_run.value.status, 202, dry_run.value.text())
        self.assertFalse(dry_run.value.json().get("submit", False))
        self.run_worker(dry_run.value.json()["operation_id"],
                        expected_receipt_state="COMPLETED")
        valid_app = self.client_application()
        self.assertEqual(valid_app["state"], "DRY_RUN_COMPLETE")
        self.assertEqual(valid_app["question_preflight"]["result"], "PASS")
        self.assertEqual(self.counts()["submit"], 0)

        self.panel.get_by_role(
            "button", name="Start a new reviewed run", exact=True).click()
        consent = self.panel.get_by_label(
            "I approve one fictional mock submission", exact=True)
        submit = self.panel.get_by_role(
            "button", name="Submit to local mock", exact=True)
        expect(consent).not_to_be_checked()
        expect(submit).to_be_disabled()
        consent.check()
        expect(submit).to_be_enabled()
        with self.page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/mock-run")) as admitted:
            submit.click()
        self.assertEqual(admitted.value.status, 202, admitted.value.text())
        submission_request = self.posts[-1]["request_id"]
        self.assertTrue(self.posts[-1]["submit"])
        submission_operation = admitted.value.json()["operation_id"]
        self.assertEqual([item["submit"] for item in self.posts],
                         [False, False, True])

        self.open_application()
        expect(self.panel).to_contain_text("QUEUED")
        self.assertEqual(len(self.posts), 3, "Reload duplicated a mock submission")
        try:
            self.run_worker(submission_operation)
        except AssertionError as error:
            failed_app = self.store.application(self.app_id)
            failed_history = read_attempt_history(self.store, "fixture", self.app_id)
            phase_summary = [{"state": item["state"], "steps": item["steps"],
                              "error_code": item["error_code"]}
                             for item in failed_history["attempts"]]
            self.fail(f"{error}; persisted_state={failed_app['state']}; "
                      f"attempt_phases={phase_summary}")
        expect(self.page.get_by_role(
            "status", name="Application state", exact=True)).to_have_text("CONFIRMED")
        final_app = self.client_application()
        self.assertEqual(final_app["state"], "CONFIRMED")
        self.assertEqual(final_app["confirmation"]["submission_count"], 1)
        self.assertEqual(self.client_receipt(submission_request)["state"], "COMPLETED")

        reopened = Store(self.store.url)
        try:
            persisted = reopened.application(self.app_id)
            self.assertEqual(persisted["state"], "CONFIRMED")
            self.assertEqual(persisted["confirmation"], final_app["confirmation"])
            history = read_attempt_history(reopened, "fixture", self.app_id)
            self.assertFalse(history["external_submission_enabled"])
            submitted = [item for item in history["attempts"]
                         if item["mode"] == "MOCK_SUBMISSION"]
            self.assertEqual(len(submitted), 1)
            attempt = submitted[0]
            self.assertEqual(attempt["state"], "CONFIRMED")
            self.assertEqual(len(attempt["verified_uploads"]), 2)
            for phase in ("NAVIGATE", "DETECT_FORM", "UPLOAD_RESUME", "FILL_CONTACT",
                          "FILL_LOCATION", "ANSWER_STANDARD", "ANSWER_CUSTOM", "VALIDATE",
                          "SUBMIT", "VERIFY_CONFIRMATION"):
                self.assertIn(f"{phase}:COMPLETED", attempt["steps"])
            self.assertEqual(attempt["confirmation"]["reference_number"],
                             final_app["confirmation"]["reference_number"])
        finally:
            reopened.engine.dispose()

        self.open_application()
        expect(self.page.get_by_role(
            "status", name="Application state", exact=True)).to_have_text("CONFIRMED")
        self.assertEqual(self.counts()["submit"], 1)
        self.assertEqual(len(self.posts), 3)
        self.assertFalse(self.external)
