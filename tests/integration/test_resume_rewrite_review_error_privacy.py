"""Resume rewrite failures stay private and preserve the reviewed source facts."""
import hashlib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import resume_rewrite_review as rewrite_api
from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.documents.errors import PreparationError
from tests.integration import test_resume_rewrite_review_api as rewrite_fixture

PRIVATE_CODE = "fictional-private-resume-history-person@example.invalid"
PRIVATE_MESSAGE = "fictional private applicant notes and proposed resume wording"
PRIVATE_CAUSE = "fictional private session context"
FALLBACK = "RESUME_REWRITE_SERVICE_ERROR"


class UnhashableCode(str):
    __hash__ = None


class ImpostorCode(str):
    def __hash__(self):
        return hash("RESUME_REWRITE_PROFILE_STALE")

    def __eq__(self, other):
        return True


def coded_error(code, error_type=PreparationError):
    if error_type is RuntimeError:
        error = error_type(PRIVATE_MESSAGE)
        error.code = code
    else:
        error = error_type(code, PRIVATE_MESSAGE)
    error.__cause__ = RuntimeError(PRIVATE_CAUSE)
    error.add_note(PRIVATE_MESSAGE)
    return error


class ResumeRewriteReviewErrorPrivacyTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real SQLite/profile/job fixture without inheriting its tests.
        self.fixture = rewrite_fixture.ResumeRewriteReviewApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        app = create_app(
            self.store, "http://127.0.0.1:1", self.fixture.root / "documents",
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        )
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def request_error(self, error):
        with patch.object(rewrite_api, "build_rewrite_review", side_effect=error) as failing:
            response = self.client.post(self.fixture.url(), json=self.fixture.request())
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
                reopened.profile("fixture"), reopened.profile("other"),
                reopened.job(self.fixture.job_id, "fixture"),
                tuple(reopened.count(table.name) for table in (
                    m.profiles, m.profile_versions, m.facts, m.applications,
                    m.documents, m.operations, m.audit,
                )),
            )
        finally:
            reopened.engine.dispose()

    def test_unknown_malformed_and_spoofed_builder_errors_are_private(self):
        before = self.reopened_state()
        codes = (
            PRIVATE_CODE,
            "RESUME_REWRITE_PROFILE_STALE:" + PRIVATE_CODE,
            "RESUME_REWRITE_NOT_FOUND:" + PRIVATE_CODE,
            " RESUME_REWRITE_INVALID", "", PRIVATE_CODE * 2048,
            None, True, 7, [PRIVATE_CODE], {"private": PRIVATE_CODE},
            PRIVATE_CODE.encode(), UnhashableCode("RESUME_REWRITE_PROFILE_STALE"),
            ImpostorCode(PRIVATE_CODE),
        )
        for index, code in enumerate(codes):
            with self.subTest(domain_case=index):
                self.assert_public(self.request_error(coded_error(code)))
        for code in (PRIVATE_CODE, "RESUME_REWRITE_PROFILE_STALE", "RESUME_REWRITE_INVALID"):
            with self.subTest(dependency_code=code):
                self.assert_public(self.request_error(coded_error(code, RuntimeError)))
        with self.subTest(dependency_without_code=True):
            self.assert_public(self.request_error(RuntimeError(PRIVATE_MESSAGE)))
        self.assertEqual(self.reopened_state(), before)

    def test_known_domain_codes_keep_their_existing_http_contracts(self):
        cases = (
            ("RESUME_REWRITE_PROFILE_STALE", 409),
            ("RESUME_REWRITE_JOB_STALE", 409),
            ("RESUME_REWRITE_JOB_INVALID", 422),
            ("RESUME_REWRITE_SELECTION_INVALID", 422),
            ("RESUME_REWRITE_INVALID", 422),
            ("RESUME_REWRITE_FACT_INVALID", 422),
        )
        for code, status in cases:
            with self.subTest(code=code):
                self.assert_public(self.request_error(coded_error(code)), status, code)

    def test_private_failure_preserves_reopened_state_and_real_review_recovers(self):
        before = self.reopened_state()
        failed = self.request_error(coded_error(PRIVATE_CODE))
        self.assertEqual(self.reopened_state(), before)
        self.assert_public(failed)

        request = self.fixture.request()
        recovered = self.client.post(self.fixture.url(), json=request)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        body = recovered.json()
        self.assertTrue(body["read_only"])
        self.assertTrue(body["review_required"])
        self.assertEqual(body["semantic_equivalence"], "USER_REVIEW_REQUIRED")
        self.assertEqual(body["profile_version"], request["expected_profile_version"])
        self.assertEqual(body["job_fingerprint"], request["expected_job_fingerprint"])
        self.assertEqual(body["selected_fact_ids"], ["experience"])
        self.assertEqual(len(body["rewrites"]), 1)
        row = body["rewrites"][0]
        self.assertEqual(row["source_text"], self.fixture.SOURCE)
        self.assertEqual(row["source_sha256"], hashlib.sha256(
            self.fixture.SOURCE.encode("utf-8")).hexdigest())
        self.assertEqual(row["proposed_text"], request["rewrites"][0]["text"])
        audit = row["protected_token_audit"]
        self.assertEqual(audit["missing"], [])
        self.assertEqual(audit["retained"], audit["required"])
        self.assertEqual({item["type"] for item in audit["required"]},
                         {"DATE", "NUMBER", "NAMED_CREDENTIAL"})
        self.assertEqual(self.reopened_state(), before)

    def test_real_stale_foreign_and_missing_context_fail_without_writes(self):
        before = self.reopened_state()
        cases = (
            (self.fixture.url(), self.fixture.request(expected_profile_version="0" * 64),
             409, "RESUME_REWRITE_PROFILE_STALE"),
            (self.fixture.url(), self.fixture.request(expected_job_fingerprint="0" * 64),
             409, "RESUME_REWRITE_JOB_STALE"),
            (self.fixture.url("other"), self.fixture.request(profile_id="other"),
             422, "RESUME_REWRITE_FACT_INVALID"),
            (self.fixture.url(), self.fixture.request(
                selected=["pending"], rewrites=[{
                    "fact_id": "pending", "text": "Synthetic changed pending wording.",
                }]), 422, "RESUME_REWRITE_FACT_INVALID"),
            (self.fixture.url("missing"), self.fixture.request(),
             404, "RESUME_REWRITE_PROFILE_NOT_FOUND"),
            (self.fixture.url(job_id="missing"), self.fixture.request(),
             404, "RESUME_REWRITE_JOB_NOT_FOUND"),
        )
        for url, request, status, code in cases:
            with self.subTest(url=url, code=code):
                self.assert_public(self.client.post(url, json=request), status, code)
        self.assertEqual(self.reopened_state(), before)
