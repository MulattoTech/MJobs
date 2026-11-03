"""Synthetic privacy bounds for FastAPI validation responses (MJ-REQ-172)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError, field_validator

from apps.api.main import create_app
from packages.persistence.store import Store


SECRET_KEY = "SYNTHETIC_PRIVATE_APPLICANT_KEY_8391"
SECRET_NOTE = "SYNTHETIC_PRIVATE_APPLICATION_ANSWER_9472"


class SensitiveFixture(BaseModel):
    items: dict[str, int]
    note: str

    @field_validator("note")
    @classmethod
    def reject_fixture(cls, value):
        raise ValueError(f"Rejected applicant answer: {value}")


class ValidationErrorPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store("sqlite:///:memory:")
        cls.store.migrate()
        cls.app = create_app(cls.store, "http://127.0.0.1:8090",
                             Path(tempfile.gettempdir()), operation_mode="PERSONAL")
        cls.handler = staticmethod(cls.app.exception_handlers[RequestValidationError])

    @classmethod
    def tearDownClass(cls):
        cls.store.engine.dispose()

    def response(self, errors, path="/synthetic-validation"):
        request = Request({
            "type": "http", "method": "POST", "path": path,
            "headers": [(b"host", b"testserver")],
            "scheme": "http", "server": ("testserver", 80), "query_string": b"",
        })
        return asyncio.run(self.handler(request, RequestValidationError(errors)))

    def test_dynamic_mapping_key_and_validator_message_never_echo(self):
        with self.assertRaises(ValidationError) as caught:
            SensitiveFixture.model_validate({
                "items": {SECRET_KEY: "invalid-integer"},
                "note": SECRET_NOTE,
            })
        errors = [
            {**entry, "loc": ("body", *entry["loc"])}
            for entry in caught.exception.errors()
        ]
        response = self.response(errors)
        self.assertEqual(response.status_code, 422)
        payload = json.loads(response.body)
        self.assertEqual(len(payload["detail"]), 2)
        self.assertNotIn(SECRET_KEY, response.body.decode())
        self.assertNotIn(SECRET_NOTE, response.body.decode())
        for item in payload["detail"]:
            self.assertEqual(set(item), {"loc", "msg", "type"})
            self.assertEqual(item["loc"][0], "body")
            self.assertIsInstance(item["msg"], str)

    def test_real_http_validation_rejects_sensitive_nested_values(self):
        async def synthetic_route(payload: SensitiveFixture):
            return {"unexpected": True}

        route = APIRoute("/__synthetic-validation", synthetic_route, methods=["POST"])
        self.app.router.routes.insert(0, route)
        try:
            with TestClient(self.app) as client:
                response = client.post("/__synthetic-validation", json={
                    "items": {SECRET_KEY: "bad-number"}, "note": SECRET_NOTE,
                })
        finally:
            self.app.router.routes.remove(route)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(SECRET_KEY, response.text)
        self.assertNotIn(SECRET_NOTE, response.text)
        self.assertEqual(len(response.json()["detail"]), 2)

    def test_error_count_location_depth_and_untrusted_types_are_bounded(self):
        errors = [
            {"loc": ("body", "items", i, SECRET_KEY, "nested", "very", "deep", "field"),
             "msg": SECRET_NOTE, "type": "evil." + SECRET_NOTE}
            for i in range(150)
        ]
        response = self.response(errors)
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 422)
        self.assertLessEqual(len(payload["detail"]), 32)
        self.assertTrue(all(len(item["loc"]) <= 6 for item in payload["detail"]))
        self.assertNotIn(SECRET_KEY, response.body.decode())
        self.assertNotIn(SECRET_NOTE, response.body.decode())

    def test_credential_path_preserves_fixed_error_code(self):
        response = self.response([
            {"loc": ("body", SECRET_KEY), "msg": SECRET_NOTE, "type": "value_error"},
        ], "/profiles/fixture/provider-credentials")
        self.assertEqual(json.loads(response.body), {
            "detail": {"code": "CREDENTIAL_INPUT_INVALID"}
        })


if __name__ == "__main__":
    unittest.main()
