"""Synthetic Lever form-review API, worker, and durable safe readback."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import make_url

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from fixtures.mock_server import fixture_server
from workers.tasks import execute_operation


LEVER_URL = "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef"
_PRIVATE_VALUES = ("PRIVATE_LEVER_EMAIL", "PRIVATE_LEVER_SSN", "PRIVATE_LEVER_ANSWER")


def _disposable_postgres_url(value):
    url = make_url(value)
    if (url.get_backend_name() != "postgresql"
            or url.host not in {"localhost", "127.0.0.1", "::1"}
            or not url.database
            or not (url.database == "mjobs_test" or url.database.startswith("mjobs_test_"))
            or url.query):
        # Query parameters can override libpq host/database/service selection.
        raise ValueError("An explicit loopback disposable mjobs_test database URL is required")
    return url


class FormReviewPostgresFixtureSafetyTests(unittest.TestCase):
    def test_rejects_non_disposable_targets_before_connecting(self):
        for value in ("sqlite:///test.db", "postgresql://localhost/mjobs",
                      "postgresql://remote.example/mjobs_test",
                      "postgresql://localhost/mjobs_test?host=remote.example",
                      "postgresql://localhost/mjobs_test?service=production"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _disposable_postgres_url(value)
        for host in ("localhost", "127.0.0.1", "[::1]"):
            self.assertEqual(_disposable_postgres_url(
                f"postgresql://{host}/mjobs_test_form_review").database,
                "mjobs_test_form_review")


class LeverFormReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-lever-review-")
        self.root = Path(self.temp.name)
        self.store = self._new_store()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(provider="lever", tenant="example",
                     requisition="01234567-89ab-cdef-0123-456789abcdef", url=LEVER_URL))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def _new_store(self):
        self.database_url = f"sqlite:///{(self.root / 'lever-review.db').as_posix()}"
        store = Store(self.database_url)
        store.migrate()
        return store

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _path(self):
        return f"/profiles/fixture/applications/{self.application_id}/form-reviews"

    def _operation_count(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.operations).where(
                m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                m.operations.c.entity_id == self.application_id,
            )).scalar_one()

    def test_lever_route_persists_safe_review_metadata_by_request_id(self):
        request_id = "lever-review-request-001"
        # Admission freezes the canonical URL; a supported persisted /apply
        # alias must still pass worker staleness checks without a second route.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url=LEVER_URL + "/apply"))
        path = self._path()
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        fetches = []

        class FakeResponse:
            url = LEVER_URL
            headers = {"content-type": "text/html; charset=utf-8"}
            content = (
                b"<form>"
                b"<label for='email'>Email</label>"
                b"<input id='email' name='email' type='email' required value='PRIVATE_LEVER_EMAIL'>"
                b"<label for='ssn'>Social Security number</label>"
                b"<input id='ssn' name='ssn' required value='PRIVATE_LEVER_SSN'>"
                b"<input id='duplicate' name='first' value='PRIVATE_LEVER_ANSWER'>"
                b"<input id='duplicate' name='second' value='PRIVATE_LEVER_ANSWER'>"
                b"</form>"
            )

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check
                self.total_timeout = total_timeout

            def get(self, url):
                self.check()
                fetches.append(url)
                if url != self.website:
                    raise AssertionError("Worker requested a different URL")
                return FakeResponse()

            def allowed(self, url):
                return url == self.website

            def close(self):
                pass

        with patch.dict("os.environ", {
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root / "documents"),
        }), patch("workers.tasks.PublicCompanyClient", FakePublicClient):
            result = execute_operation(operation_id)

        self.assertEqual(fetches, [LEVER_URL])
        self.assertEqual(result["status"], "COMPLETED")
        read = self.client.get(path + "/" + request_id)
        self.assertEqual(read.status_code, 200, read.text)
        payload = read.json()
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["state"], "COMPLETED")
        analysis = payload["analysis"]
        self.assertEqual(analysis["status"], "REVIEW_REQUIRED")
        self.assertEqual({field["name"] for field in analysis["fields"]},
                         {"email", "ssn", "first", "second"})
        review_reasons = {field["review_reason"] for field in analysis["fields"]}
        self.assertIn("SENSITIVE_FIELD", review_reasons)
        self.assertIn("AMBIGUOUS_FIELD", review_reasons)
        for secret in _PRIVATE_VALUES:
            self.assertNotIn(secret, read.text)

        with self.store.engine.connect() as connection:
            audits = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings())
        self.assertTrue(audits)
        serialized_audit = str([row["payload"] for row in audits])
        for secret in _PRIVATE_VALUES:
            self.assertNotIn(secret, serialized_audit)
        audit = next(row["payload"] for row in audits
                     if row["payload"].get("operation_id") == operation_id)
        self.assertEqual(audit["analysis"], analysis)
        cross_profile = self.client.get(
            f"/profiles/other-profile/applications/{self.application_id}/form-reviews/{request_id}"
        )
        self.assertEqual(cross_profile.status_code, 404)

    def test_malformed_or_unsupported_routes_do_not_enqueue_or_fetch(self):
        path = self._path()
        original_count = self._operation_count()

        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url="https://jobs.lever.co.attacker.test/example/01234567-89ab-cdef-0123-456789abcdef"))
        malformed = self.client.post(path, json={"request_id": "lever-malformed-route"})
        self.assertEqual(malformed.status_code, 409, malformed.text)
        self.assertEqual(self._operation_count(), original_count)

        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(provider="unsupported", url="https://unsupported.example/jobs/123"))
        unsupported = self.client.post(path, json={"request_id": "lever-unsupported-route"})
        self.assertEqual(unsupported.status_code, 409, unsupported.text)
        self.assertEqual(self._operation_count(), original_count)

    def test_exact_retry_preserves_operation_state_and_recovery_metadata(self):
        for state in ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "DEAD_LETTERED",
                      "CANCELLED", "DISCARDED", "BLOCKED"):
            with self.subTest(state=state):
                body = {"request_id": "retry-preserves-" + state.lower()}
                first = self.client.post(self._path(), json=body)
                self.assertEqual(first.status_code, 202, first.text)
                operation_id = first.json()["operation_id"]
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(
                        m.operations.c.id == operation_id,
                    ).values(state=state, attempt_count=2, progress=40,
                             current_step="FETCHING_FORM", retry_count=1,
                             recovery_metadata={"checkpoint": "test"},
                             cancel_requested=state == "CANCELLED"))
                    before = dict(connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id)).mappings().one())

                retry = self.client.post(self._path(), json=body)

                self.assertEqual(retry.status_code, 202, retry.text)
                self.assertEqual(retry.json(), {**first.json(), "state": state})
                with self.store.engine.connect() as connection:
                    self.assertEqual(dict(connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id)).mappings().one()), before)

    def test_request_id_reuse_after_route_change_conflicts_without_new_work(self):
        body = {"request_id": "route-bound-inspection"}
        first = self.client.post(self._path(), json=body)
        self.assertEqual(first.status_code, 202, first.text)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url=LEVER_URL.replace("01234567", "11234567"),
                     requisition="11234567-89ab-cdef-0123-456789abcdef"))
        retry = self.client.post(self._path(), json=body)
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(retry.json()["detail"]["code"], "FORM_REVIEW_IDEMPOTENCY_CONFLICT")
        self.assertEqual(self._operation_count(), 1)
        self.assertEqual(self.client.get(self._path() + "/" + body["request_id"]).json()["state"],
                         "QUEUED")
        fresh = self.client.post(self._path(), json={"request_id": "fresh-route-inspection"})
        self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assertNotEqual(fresh.json()["operation_id"], first.json()["operation_id"])

    def test_concurrent_retries_reserve_one_request_identity(self):
        body = {"request_id": "concurrent-inspection"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post(self._path(), json=body), range(4)))
        self.assertEqual([r.status_code for r in responses], [202] * 4)
        self.assertEqual(len({r.json()["operation_id"] for r in responses}), 1)
        self.assertEqual(self._operation_count(), 1)
        profile = self.store.profile("fixture")
        profile["name"] = "Concurrent Synthetic Edit"
        self.store.save_profile(profile)
        bodies = [body, {"request_id": "new-concurrent-inspection"}] * 2
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda value: self.client.post(self._path(), json=value), bodies))
        self.assertEqual([r.status_code for r in responses], [409, 202, 409, 202])
        self.assertEqual(responses[1].json()["operation_id"], responses[3].json()["operation_id"])
        self.assertEqual(self._operation_count(), 2)

    def test_legacy_duplicate_request_identity_fails_closed(self):
        from workers.dispatch import enqueue_operation

        body = {"request_id": "legacy-ambiguous-inspection"}
        first = self.client.post(self._path(), json=body)
        self.assertEqual(first.status_code, 202, first.text)
        with self.store.engine.connect() as connection:
            params = dict(connection.execute(select(m.operations.c.params).where(
                m.operations.c.id == first.json()["operation_id"])).scalar_one())
        # Simulate the two rows older admission code could persist for one ID.
        params["profile_version"] = "0" * 64
        enqueue_operation(self.store, "APPLICATION_FORM_REVIEW", self.application_id, params)
        self.assertEqual(self._operation_count(), 2)
        for response in (self.client.post(self._path(), json=body),
                         self.client.get(self._path() + "/" + body["request_id"])):
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "FORM_REVIEW_IDEMPOTENCY_CONFLICT")
        self.assertEqual(self._operation_count(), 2)


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database")
class LeverFormReviewPostgresFlowTests(LeverFormReviewFlowTests):
    """Run the same connected/replay/concurrency acceptance in a private test schema."""

    def _new_store(self):
        url = _disposable_postgres_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        self.schema = "form_review_" + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self._drop_schema)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.database_url = url.update_query_dict({
            "options": "-csearch_path=" + self.schema + " -clock_timeout=5000 -cstatement_timeout=20000",
        }).render_as_string(hide_password=False)
        store = Store(self.database_url)
        self.addCleanup(store.engine.dispose)
        # Match Store.migrate's metadata registration; never migrate/reset a shared DB.
        from packages.persistence import resume_workspace  # noqa: F401
        m.metadata.create_all(store.engine)
        return store

    def _drop_schema(self):
        try:
            # The identifier is generated above, never supplied by a URL or fixture.
            if not (self.schema.startswith("form_review_")
                    and len(self.schema) == 44
                    and all(c in "0123456789abcdef" for c in self.schema[12:])):
                raise ValueError("Unexpected disposable schema identifier")
            with self.admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            self.admin.dispose()


if __name__ == "__main__":
    unittest.main()
