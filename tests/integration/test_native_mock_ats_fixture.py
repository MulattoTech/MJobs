"""Native provider-shaped fixture routes stay local and bind durable receipts."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from fixtures.mock_server import create_mock_app

RESUME = b"%PDF-1.4\nsynthetic resume\n%%EOF"
COVER_LETTER = b"%PDF-1.4\nsynthetic cover letter\n%%EOF"
APP_ID = "fixture-native-application-0001"
REQUEST_ID = "fixture-native-request-0001"


def contract_hash(client, provider="greenhouse"):
    response = client.get(f"/ats/native/{provider}/B/questions")
    assert response.status_code == 200, response.text
    return response.json()["contract_sha256"]


def post_form(client, *, provider="greenhouse", request_id=REQUEST_ID,
              application_id=APP_ID, contract_sha=None, resume=RESUME,
              cover=COVER_LETTER, **changes):
    if contract_sha is None:
        contract_sha = contract_hash(client, provider)
    fields = {
        "name": "Alex Fixture",
        "email": "alex@example.test",
        "country": "United States",
        "remote": "yes",
        "fixture_question": "Controlled fictional test application.",
        "application_id": application_id,
        "request_id": request_id,
        "contract_sha256": contract_sha,
    }
    fields.update(changes)
    files = {
        "resume": ("resume.pdf", resume, "application/pdf"),
        "cover_letter": ("cover-letter.pdf", cover, "application/pdf"),
    }
    return client.post(f"/ats/native/{provider}/B/submit", data=fields, files=files)


class NativeMockATSFixtureTests(unittest.TestCase):
    def test_native_scenario_exposes_one_accessible_form_and_question_contract(self):
        with TestClient(create_mock_app(scenario="native-greenhouse")) as client:
            page = client.get("/ats/native/greenhouse/B")
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.text.count("<form"), 1)
            self.assertNotIn("<script", page.text.lower())
            self.assertNotIn("<iframe", page.text.lower())
            for label in ("Name", "Email", "Country", "Remote role", "Resume",
                          "Cover letter", "Approved fixture answer"):
                self.assertIn(f'aria-label="{label}"', page.text)
            self.assertIn('action="/ats/native/greenhouse/B/submit"', page.text)
            self.assertIn("Submit owned fixture", page.text)

            questions = client.get("/ats/native/greenhouse/B/questions")
            old_questions = client.get("/ats/questions/B")
            self.assertEqual(questions.status_code, 200)
            self.assertEqual(questions.json()["questions"], old_questions.json()["questions"])
            self.assertEqual(questions.json()["template_version"], "native-form-v1")
            self.assertEqual(len(questions.json()["contract_sha256"]), 64)

    def test_routes_are_opt_in_and_provider_scoped(self):
        with TestClient(create_mock_app()) as baseline:
            self.assertEqual(baseline.get("/ats/native/greenhouse/B").status_code, 404)
        with TestClient(create_mock_app(scenario="native-greenhouse")) as client:
            self.assertEqual(client.get("/ats/native/lever/B").status_code, 404)
            self.assertEqual(client.get("/ats/native/greenhouse/C").status_code, 404)
            jobs = client.get("/ats/example/jobs").json()["jobs"]
            self.assertEqual([row["source_job_id"] for row in jobs], ["employer-A", "employer-B"])
            self.assertTrue(jobs[0]["application_url"].endswith("/ats/apply/A"))
            self.assertTrue(jobs[1]["application_url"].endswith("/ats/native/greenhouse/B"))

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown fictional fixture scenario"):
            create_mock_app(scenario="native-workday")

    def test_valid_submission_binds_files_and_survives_fixture_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt_path = Path(temp) / "receipts.json"
            with TestClient(create_mock_app(receipt_path, scenario="native-greenhouse")) as client:
                response = post_form(client)
                self.assertEqual(response.status_code, 200, response.text)
                receipt = client.get(f"/ats/receipts/{APP_ID}").json()
                self.assertEqual(receipt["native_provider"], "greenhouse")
                self.assertEqual(receipt["request_id"], REQUEST_ID)
                self.assertEqual(receipt["contract_sha256"], contract_hash(client))
                self.assertEqual(receipt["resume_sha256"], hashlib.sha256(RESUME).hexdigest())
                self.assertEqual(receipt["cover_letter_sha256"], hashlib.sha256(COVER_LETTER).hexdigest())
                self.assertEqual(receipt["submission_count"], 1)

                replay = post_form(client)
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(client.get(f"/ats/receipts/{APP_ID}").json(), receipt)
                changed_binding = post_form(client, request_id="another-request")
                self.assertEqual(changed_binding.status_code, 409)

            with TestClient(create_mock_app(receipt_path, scenario="native-greenhouse")) as restarted:
                self.assertEqual(restarted.get(f"/ats/receipts/{APP_ID}").json(), receipt)
                self.assertEqual(post_form(restarted).status_code, 200)
                self.assertEqual(restarted.get(f"/ats/receipts/{APP_ID}").json(), receipt)
            self.assertEqual(len(json.loads(receipt_path.read_text(encoding="utf-8"))), 1)

    def test_missing_fields_wrong_profile_values_invalid_pdf_and_stale_bindings_reject(self):
        with TestClient(create_mock_app(scenario="native-greenhouse")) as client:
            missing = {
                "name": "", "email": "", "country": "", "remote": "",
                "fixture_question": "", "request_id": "", "application_id": "",
            }
            self.assertEqual(post_form(client, **missing).status_code, 422)
            self.assertEqual(post_form(client, email="other@example.test").status_code, 422)
            self.assertEqual(post_form(client, country="Canada").status_code, 422)
            self.assertEqual(post_form(client, fixture_question="unapproved answer").status_code, 422)
            self.assertEqual(post_form(client, resume=b"not a pdf").status_code, 422)
            self.assertEqual(post_form(client, cover=b"not a pdf").status_code, 422)
            self.assertEqual(post_form(client, contract_sha="0" * 64).status_code, 409)
            self.assertEqual(client.get(f"/ats/receipts/{APP_ID}").json(), {"status": "not_found"})

    def test_conflicting_existing_receipt_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt_path = Path(temp) / "receipts.json"
            receipt_path.write_text(json.dumps({APP_ID: {
                "application_id": APP_ID, "requisition": "A", "receipt": "MOCK-existing",
            }}), encoding="utf-8")
            with TestClient(create_mock_app(receipt_path, scenario="native-greenhouse")) as client:
                response = post_form(client)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(client.get(f"/ats/receipts/{APP_ID}").json()["receipt"], "MOCK-existing")


if __name__ == "__main__":
    unittest.main()
