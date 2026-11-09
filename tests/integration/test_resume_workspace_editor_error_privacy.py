"""Editor HTTP failures expose fixed codes without changing reviewed data."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import resume_workspace_editor as editor
from packages.persistence.store import Store
from services.documents.errors import PreparationError
from services.documents.resume_workspace import ResumeWorkspaceError
from tests.integration import test_resume_workspace_editor_api as editor_fixture

PRIVATE_CODE = "fictional-private-resume-diagnostic"
PRIVATE_MESSAGE = "fictional applicant notes: person@example.invalid"
PRIVATE_CAUSE = "fictional private session context"
FALLBACK = "RESUME_WORKSPACE_SERVICE_ERROR"


class ImpostorCode(str):
    __hash__ = None

    def __eq__(self, other):
        return True


def coded_error(code, error_type=ResumeWorkspaceError):
    if error_type is RuntimeError:
        error = error_type(PRIVATE_MESSAGE)
        error.code = code
    else:
        error = error_type(code, PRIVATE_MESSAGE)
    error.__cause__ = RuntimeError(PRIVATE_CAUSE)
    error.add_note(PRIVATE_MESSAGE)
    return error


class ResumeWorkspaceEditorErrorPrivacyTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real SQLite/profile/job setup, without inheriting its tests.
        editor_fixture.ResumeWorkspaceEditorApiTests.setUp(self)
        self.client = TestClient(self.client.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        preview = self.client.post(self.path + "/preview", json=self.preview_body)
        self.assertEqual(preview.status_code, 200)
        self.save_body = {
            "expected_version": None,
            "profile_version": self.profile["version"],
            "job_fingerprint": self.job_fingerprint,
            "template_id": self.template["template_id"],
            "sections": self.sections,
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "reviewed": True,
            "request_id": "privacy-regression-reviewed-save",
        }
        saved = self.client.put(self.path, json=self.save_body)
        self.assertEqual(saved.status_code, 200)
        self.revision = saved.json()["workspace_revision"]
        self.routes = {
            "history": ("list_resume_workspace_revisions", "GET", self.path, None),
            "preview": ("build_resume_workspace_draft_preview", "POST", self.path + "/preview", self.preview_body),
            "save": ("save_reviewed_resume_workspace_export", "PUT", self.path, self.save_body),
            "text": ("read_reviewed_resume_workspace_export", "GET", self.path + "/versions/1/text", None),
            **{kind: ("render_reviewed_resume_workspace_export", "GET",
                      self.path + "/versions/1/" + kind, None)
               for kind in ("txt", "docx", "pdf")},
        }

    def request_error(self, route, error):
        service, method, path, payload = self.routes[route]
        with patch.object(editor, service, side_effect=error) as failing:
            response = self.client.request(method, path, json=payload)
        failing.assert_called_once()
        return response

    def assert_public(self, response, status=500, code=FALLBACK):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.headers.get("content-type"), "application/json")
        self.assertEqual(response.json(), {"detail": {"code": code}})
        self.assertLess(len(response.content), 128)
        for private in (PRIVATE_CODE, PRIVATE_MESSAGE, PRIVATE_CAUSE):
            self.assertNotIn(private, response.text)

    def reopened_state(self):
        reopened = Store(self.store.url)
        try:
            return (
                reopened.profile("fixture"), reopened.job(self.job["id"], "fixture"),
                editor.list_resume_workspace_revisions(reopened, "fixture", self.job["id"]),
                tuple(reopened.count(table) for table in ("applications", "documents", "worker_operations")),
            )
        finally:
            reopened.engine.dispose()

    def test_unknown_domain_and_dependency_codes_are_private_on_every_route(self):
        for route in self.routes:
            for error_type in (ResumeWorkspaceError, PreparationError, RuntimeError):
                with self.subTest(route=route, error_type=error_type.__name__):
                    self.assert_public(self.request_error(route, coded_error(PRIVATE_CODE, error_type)))

    def test_misleading_and_malformed_codes_cannot_choose_a_public_status(self):
        codes = (
            "RESUME_WORKSPACE_PROFILE_STALE:" + PRIVATE_CODE,
            "RESUME_WORKSPACE_REQUEST_CONFLICT:" + PRIVATE_CODE,
            "RESUME_WORKSPACE_NOT_FOUND:" + PRIVATE_CODE,
            " RESUME_WORKSPACE_INVALID", "", PRIVATE_CODE * 2048,
            None, True, 7, [], {}, [PRIVATE_CODE], {"private": PRIVATE_CODE},
        )
        for index, code in enumerate(codes):
            with self.subTest(case=index):
                self.assert_public(self.request_error("preview", coded_error(code, PreparationError)))

    def test_dependency_codes_and_string_subclasses_cannot_impersonate_domain_errors(self):
        for route in self.routes:
            for code in ("RESUME_WORKSPACE_INVALID", "RESUME_WORKSPACE_NOT_FOUND",
                         "RESUME_WORKSPACE_PROFILE_STALE"):
                with self.subTest(route=route, dependency_code=code):
                    self.assert_public(self.request_error(route, coded_error(code, RuntimeError)))
            with self.subTest(route=route, code_type="impostor_string"):
                self.assert_public(self.request_error(route, coded_error(ImpostorCode(PRIVATE_CODE))))

    def test_known_domain_codes_keep_their_existing_http_contracts(self):
        cases = (
            ("preview", PreparationError, "RESUME_WORKSPACE_PROFILE_STALE", 409),
            ("preview", PreparationError, "RESUME_WORKSPACE_JOB_STALE", 409),
            ("preview", PreparationError, "RESUME_WORKSPACE_STALE_CONTEXT", 409),
            ("preview", PreparationError, "RESUME_TEMPLATE_VERSION_STALE", 409),
            ("preview", PreparationError, "RESUME_TEMPLATE_SELECTION_INVALID", 500),
            ("preview", PreparationError, "RESUME_TEMPLATE_VERSION_REQUIRED", 500),
            ("preview", PreparationError, "RESUME_WORKSPACE_PREVIEW_INVALID", 500),
            ("save", ResumeWorkspaceError, "RESUME_WORKSPACE_INVALID", 422),
            ("save", ResumeWorkspaceError, "RESUME_WORKSPACE_REVIEW_REQUIRED", 422),
            ("save", ResumeWorkspaceError, "RESUME_WORKSPACE_PREVIEW_STALE", 409),
            ("save", ResumeWorkspaceError, "RESUME_WORKSPACE_REQUEST_CONFLICT", 409),
            ("save", ResumeWorkspaceError, "RESUME_WORKSPACE_STALE_CONTEXT", 409),
            ("save", ResumeWorkspaceError, "RESUME_WORKSPACE_STALE_VERSION", 409),
            ("text", ResumeWorkspaceError, "RESUME_WORKSPACE_NOT_FOUND", 404),
            ("txt", PreparationError, "AVOID_LIST_BLOCKED", 409),
            ("pdf", PreparationError, "DOCUMENT_FONT_UNAVAILABLE", 500),
            ("pdf", PreparationError, "DOCUMENT_UNSUPPORTED_GLYPH", 500),
            ("docx", PreparationError, "DOCUMENT_CONTENT_MISMATCH", 500),
        )
        for route, error_type, code, status in cases:
            with self.subTest(route=route, code=code):
                self.assert_public(self.request_error(route, coded_error(code, error_type)), status, code)
        for route in ("history", "text"):
            with self.subTest(route=route, missing_scope=True):
                self.assert_public(self.request_error(route, coded_error("RESUME_WORKSPACE_INVALID")),
                                   404, "RESUME_WORKSPACE_NOT_FOUND")
        self.assert_public(self.request_error("txt", coded_error("RESUME_WORKSPACE_INVALID")),
                           422, "RESUME_WORKSPACE_INVALID")

    def test_failures_preserve_reviewed_state_and_real_preview_save_export_recover(self):
        before = self.reopened_state()
        for route in self.routes:
            self.assert_public(self.request_error(route, coded_error(PRIVATE_CODE)))
        self.assertEqual(self.reopened_state(), before)
        history = self.client.get(self.path)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["current"], self.revision)
        preview = self.client.post(self.path + "/preview", json=self.preview_body)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["preview_fingerprint"], self.save_body["preview_fingerprint"])
        saved = self.client.put(self.path, json=self.save_body)
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["replayed"])
        exported = self.client.get(self.path + "/versions/1/text")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.text, self.revision["rendered_text"])
        self.assertEqual(self.reopened_state(), before)
