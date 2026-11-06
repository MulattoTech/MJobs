"""Public resume-preview errors must not serialize arbitrary service diagnostics."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.resume_workspace_preview import create_resume_workspace_preview_router
from packages.persistence import models as m
from packages.persistence.resume_workspace import resume_workspace_revisions
from packages.persistence.store import Store
from services.documents.errors import PreparationError
from services.documents.resume_workspace_preview import build_resume_workspace_preview
from tests.integration import test_resume_workspace_preview_api as preview_fixture

PRIVATE_CODE = "fictional-private-resume-diagnostic"
PRIVATE_MESSAGE = "fictional applicant notes: person@example.invalid"
PRIVATE_CAUSE = "fictional private source context"
FALLBACK = {"detail": {"code": "RESUME_WORKSPACE_PREVIEW_INVALID"}}
BUILDER = "services.documents.resume_workspace_preview.build_resume_workspace_preview"


def coded_error(kind, code):
    if kind == "preparation":
        error = PreparationError(code, PRIVATE_MESSAGE)
    else:
        error = ValueError(PRIVATE_MESSAGE)
        error.code = code
    error.__cause__ = RuntimeError(PRIVATE_CAUSE)
    error.add_note(PRIVATE_MESSAGE)
    return error


class ResumeWorkspacePreviewErrorPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preview_fixture.ResumeWorkspacePreviewApiTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.client.close)
        app = FastAPI()
        app.include_router(create_resume_workspace_preview_router(self.fixture.store))
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def post_error(self, error):
        with patch(BUILDER, side_effect=error):
            return self.client.post(self.fixture.path, json=self.fixture.payload)

    def assert_private(self, response):
        self.assertNotIn(PRIVATE_CODE, response.text)
        self.assertNotIn(PRIVATE_MESSAGE, response.text)
        self.assertNotIn(PRIVATE_CAUSE, response.text)
        self.assertLess(len(response.content), 128)

    def assert_fallback(self, response):
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.json() == FALLBACK, "Expected fixed public error envelope")
        self.assert_private(response)

    def durable_state(self, store):
        with store.engine.connect() as connection:
            counts = tuple(connection.execute(select(func.count()).select_from(table)).scalar_one()
                           for table in (m.applications, m.documents, m.operations))
            revisions = [dict(row) for row in connection.execute(
                select(resume_workspace_revisions).order_by(resume_workspace_revisions.c.id)
            ).mappings()]
        return (
            counts, revisions, store.profile("fixture"),
            store.job(self.fixture.job["id"], "fixture"),
        )

    def reopened_state(self):
        reopened = Store(self.fixture.store.url)
        try:
            return self.durable_state(reopened)
        finally:
            reopened.engine.dispose()

    def test_unrecognized_string_codes_are_bounded_private_responses(self):
        codes = (
            PRIVATE_CODE, "", "UNRECOGNIZED_DOCUMENT_ERROR",
            "RESUME_WORKSPACE_PROFILE_STALE:" + PRIVATE_CODE,
            " RESUME_TEMPLATE_VERSION_STALE", PRIVATE_CODE * 2048,
        )
        for kind in ("preparation", "value"):
            for index, code in enumerate(codes):
                with self.subTest(kind=kind, case=index):
                    self.assert_fallback(self.post_error(coded_error(kind, code)))

    def test_nonstring_codes_do_not_escape_as_server_errors_or_response_data(self):
        codes = (None, True, 7, 10 ** 400, float("nan"), [], {}, [PRIVATE_CODE],
                 {"private": PRIVATE_CODE}, (PRIVATE_CODE,), b"private diagnostic")
        for kind in ("preparation", "value"):
            for index, code in enumerate(codes):
                with self.subTest(kind=kind, case=index):
                    self.assert_fallback(self.post_error(coded_error(kind, code)))

    def test_documented_codes_preserve_conflict_and_validation_contracts(self):
        cases = (
            ("RESUME_WORKSPACE_PROFILE_STALE", 409),
            ("RESUME_WORKSPACE_JOB_STALE", 409),
            ("RESUME_WORKSPACE_STALE_CONTEXT", 409),
            ("RESUME_TEMPLATE_VERSION_STALE", 409),
            ("RESUME_TEMPLATE_SELECTION_INVALID", 422),
            ("RESUME_TEMPLATE_VERSION_REQUIRED", 422),
            ("RESUME_WORKSPACE_PREVIEW_INVALID", 422),
        )
        for kind in ("preparation", "value"):
            for code, status in cases:
                with self.subTest(kind=kind, code=code):
                    response = self.post_error(coded_error(kind, code))
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json(), {"detail": {"code": code}})
                    self.assert_private(response)

    def test_existing_missing_unavailable_and_generic_validation_errors_stay_private(self):
        cases = (
            (LookupError(PRIVATE_MESSAGE), 404, "RESUME_WORKSPACE_NOT_FOUND"),
            (RuntimeError(PRIVATE_MESSAGE), 503, "RESUME_WORKSPACE_PREVIEW_UNAVAILABLE"),
            (ValueError(PRIVATE_MESSAGE), 422, "RESUME_WORKSPACE_PREVIEW_INVALID"),
        )
        for error, status, code in cases:
            with self.subTest(error_type=type(error).__name__):
                response = self.post_error(error)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"detail": {"code": code}})
                self.assert_private(response)

    def test_faults_preserve_reopened_saved_data_and_real_preview_recovers(self):
        before = self.reopened_state()
        expected = build_resume_workspace_preview(
            self.fixture.store, "fixture", self.fixture.job["id"], **self.fixture.payload)
        faults = (
            coded_error("preparation", PRIVATE_CODE),
            coded_error("value", {"private": PRIVATE_CODE}),
        )
        for error in faults:
            self.assert_fallback(self.post_error(error))
            self.assertEqual(self.reopened_state(), before)
        recovered = self.client.post(self.fixture.path, json=self.fixture.payload)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json(), expected)
        self.assertEqual(self.reopened_state(), before)

    def test_invalid_service_readback_still_uses_its_fixed_server_error(self):
        for result in (None, {"profile_id": PRIVATE_CODE, "preview_text": PRIVATE_MESSAGE}):
            with self.subTest(result_type=type(result).__name__):
                with patch(BUILDER, return_value=result):
                    response = self.client.post(self.fixture.path, json=self.fixture.payload)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(),
                                 {"detail": {"code": "RESUME_WORKSPACE_PREVIEW_READBACK_INVALID"}})
                self.assert_private(response)
