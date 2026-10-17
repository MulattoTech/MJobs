"""The explicit completion API never treats closure or arbitrary JSON as auth."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.manual_auth import create_manual_auth_router


class CompletionAdmissionTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(create_manual_auth_router(object(), "http://127.0.0.1:1", ".", "FIXTURE"))
        self.client = TestClient(app)
        self.app = app
        self.addCleanup(self.client.close)
        self.path = "/profiles/p/applications/a/manual-auth/reviews/r/completion"
        self.body = {
            "user_completed_authentication": True,
            "expected_route_url": "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/US/Engineer_R1",
            "expires_in_seconds": 900,
        }

    def test_completion_requires_explicit_boolean_and_bounded_lifetime(self):
        for changes in (
            {"user_completed_authentication": False},
            {"user_completed_authentication": "true"},
            {"user_completed_authentication": 1},
            {"expires_in_seconds": 0},
            {"expires_in_seconds": 901},
            {"expires_in_seconds": True},
        ):
            with self.subTest(changes=changes):
                response = self.client.put(self.path, json={**self.body, **changes})
                self.assertEqual(response.status_code, 422, response.text)

    def test_credentials_and_session_payloads_are_not_accepted(self):
        for field in ("password", "cookies", "access_token", "browser_profile", "authenticated"):
            with self.subTest(field=field):
                response = self.client.put(self.path, json={**self.body, field: "PRIVATE_SENTINEL"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("PRIVATE_SENTINEL", response.text)

    def test_non_object_input_is_rejected_without_echoing_values(self):
        for body in (["PRIVATE_SENTINEL"], "PRIVATE_SENTINEL", 123, True):
            with self.subTest(body=body):
                response = self.client.put(self.path, json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("PRIVATE_SENTINEL", response.text)

    def test_omitted_completion_signal_is_not_window_closure(self):
        body = {key: value for key, value in self.body.items() if key != "user_completed_authentication"}
        self.assertEqual(self.client.put(self.path, json=body).status_code, 422)

    def test_public_schema_documents_explicit_declaration_contract(self):
        path = "/profiles/{profile_id}/applications/{application_id}/manual-auth/reviews/{review_operation_id}/completion"
        operation = self.app.openapi()["paths"][path]["put"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("user_completed_authentication", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["expires_in_seconds"]["maximum"], 900)
        self.assertIn("never proof", operation["description"])
