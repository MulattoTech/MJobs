"""Connected form-review API, durable worker, and safe audit readback."""
from pathlib import Path
import gc
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class ApplicationFormReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-form-review-")
        self.root = Path(self.temp.name)
        self.store = self._new_store()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(self.application_id)
        self.route_url = "https://boards.greenhouse.io/example/jobs/12345"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(provider="greenhouse", tenant="example", requisition="12345", url=self.route_url))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def _new_store(self):
        self.database_url = f"sqlite:///{(self.root / 'form-review.db').as_posix()}"
        store = Store(self.database_url)
        store.migrate()
        return store

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _complete_review(self, request_id, field_name="review_field"):
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        html = (f"<form><input id='{field_name}' name='{field_name}' type='text'></form>").encode()

        class FakeResponse:
            headers = {"content-type": "text/html; charset=utf-8"}
            content = html

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check

            def get(self, url):
                self.check()
                if url != self.website:
                    raise AssertionError("Worker requested a different URL")
                response = FakeResponse()
                response.url = url
                return response

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
        self.assertEqual(result["status"], "COMPLETED")
        return path

    def test_latest_hides_old_analysis_after_profile_version_changes_but_request_id_is_historical(self):
        request_id = "freshness-profile-version"
        path = self._complete_review(request_id, "old_profile_field")
        exact_path = f"{path}/{request_id}"
        original = self.client.get(exact_path)
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(original.json()["analysis"]["fields"][0]["name"], "old_profile_field")

        profile = self.store.profile("fixture")
        previous_version = profile["version"]
        profile["name"] = "Updated Fixture Profile"
        current_version = self.store.save_profile(profile, expected_version=previous_version)
        self.assertNotEqual(current_version, previous_version)

        latest = self.client.get(path + "/latest")

        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["state"], "UNKNOWN")
        self.assertIsNone(latest.json()["request_id"])
        self.assertIsNone(latest.json()["analysis"])
        self.assertIsNone(latest.json()["error_code"])
        self.assertNotIn("old_profile_field", latest.text)
        historical = self.client.get(exact_path)
        self.assertEqual(historical.status_code, 200, historical.text)
        self.assertEqual(historical.json()["state"], "COMPLETED")
        self.assertEqual(historical.json()["analysis"]["fields"][0]["name"], "old_profile_field")

        self._complete_review("freshness-profile-updated", "current_profile_field")
        current = self.client.get(path + "/latest")
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["state"], "COMPLETED")
        self.assertEqual(current.json()["analysis"]["fields"][0]["name"], "current_profile_field")

    def test_request_id_reuse_cannot_replace_completed_profile_snapshot(self):
        request_id = "immutable-profile-review"
        path = self._complete_review(request_id, "original_review_field")
        original = self.client.get(path + "/" + request_id).json()
        with self.store.engine.connect() as connection:
            before = [dict(row) for row in connection.execute(select(m.operations)).mappings()]
        profile = self.store.profile("fixture")
        profile["name"] = "Changed Synthetic Applicant"
        self.store.save_profile(profile)

        retry = self.client.post(path, json={"request_id": request_id})

        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(retry.json()["detail"]["code"], "FORM_REVIEW_IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.client.get(path + "/" + request_id).json(), original)
        with self.store.engine.connect() as connection:
            self.assertEqual([dict(row) for row in connection.execute(select(m.operations)).mappings()], before)
        self.client.close()
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                           operation_mode="FIXTURE"))
        self.assertEqual(self.client.get(path + "/" + request_id).json(), original)
        self._complete_review("new-profile-review", "new_review_field")
        self.assertEqual(self.client.get(path + "/latest").json()["analysis"]["fields"][0]["name"],
                         "new_review_field")
        self.assertEqual(self.client.get(path + "/" + request_id).json(), original)

        # Reopened TestClients can retain closed sqlite cursor cycles on Windows.
        # Dispose this test's pool and collect them before removing its temp DB.
        self.store.engine.dispose()
        gc.collect()

    def test_latest_hides_old_analysis_after_route_id_changes(self):
        path = self._complete_review("freshness-route-id", "old_route_field")
        application = self.store.application(self.application_id)
        with self.store.engine.connect() as connection:
            old_route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"],
            )).mappings().one())
        new_route_id = "form-review-replacement-route"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == old_route["id"],
            ).values(tenant="retired", requisition="retired-12345"))
            connection.execute(m.routes.insert().values(
                id=new_route_id,
                job_id=old_route["job_id"],
                company_id=old_route["company_id"],
                provider=old_route["provider"],
                tenant=old_route["tenant"],
                requisition=old_route["requisition"],
                url=old_route["url"],
            ))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(route_id=new_route_id))

        latest = self.client.get(path + "/latest")

        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["state"], "UNKNOWN")
        self.assertIsNone(latest.json()["analysis"])
        self.assertNotIn("old_route_field", latest.text)

    def test_latest_hides_old_analysis_after_provider_and_canonical_url_change(self):
        path = self._complete_review("freshness-provider-url", "old_provider_field")
        route_id = self.store.application(self.application_id)["route_id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == route_id,
            ).values(
                provider="lever",
                tenant="example",
                requisition="123e4567-e89b-12d3-a456-426614174000",
                url="https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000",
            ))

        latest = self.client.get(path + "/latest")

        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["state"], "UNKNOWN")
        self.assertIsNone(latest.json()["analysis"])
        self.assertNotIn("old_provider_field", latest.text)

    def test_latest_hides_old_analysis_after_canonical_url_changes_with_same_provider(self):
        path = self._complete_review("freshness-canonical-url", "old_canonical_field")
        route_id = self.store.application(self.application_id)["route_id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == route_id,
            ).values(
                requisition="54321",
                url="https://boards.greenhouse.io/example/jobs/54321",
            ))

        latest = self.client.get(path + "/latest")

        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["state"], "UNKNOWN")
        self.assertIsNone(latest.json()["analysis"])
        self.assertNotIn("old_canonical_field", latest.text)

    def test_alias_only_route_change_keeps_normalized_latest_analysis_current(self):
        path = self._complete_review("freshness-route-alias", "current_alias_field")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url="https://job-boards.greenhouse.io/example/jobs/12345?source=fixture"))

        latest = self.client.get(path + "/latest")

        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["state"], "COMPLETED")
        self.assertEqual(latest.json()["analysis"]["fields"][0]["name"], "current_alias_field")

    def test_explicit_request_runs_read_only_review_and_persists_only_safe_result(self):
        request_id = "review-request-001"
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]

        class FakeResponse:
            url = self.route_url
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"<form><label for='email'>Email</label><input id='email' name='email' type='email' required value='PRIVATE_VALUE'><input id='ssn' name='ssn' type='text'></form>"

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check
                self.total_timeout = total_timeout
            def get(self, url):
                self.check()
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

        self.assertEqual(result["status"], "COMPLETED")
        read = self.client.get(path + "/" + request_id)
        self.assertEqual(read.status_code, 200, read.text)
        payload = read.json()
        self.assertEqual(payload["state"], "COMPLETED")
        self.assertEqual(payload["analysis"]["status"], "REVIEW_REQUIRED")
        self.assertTrue(any(field["review_reason"] == "SENSITIVE_FIELD" for field in payload["analysis"]["fields"]))
        self.assertNotIn("PRIVATE_VALUE", read.text)
        self.assertNotIn("value", read.text.casefold())
        self.assertEqual(self.client.get(
            f"/profiles/other-profile/applications/{self.application_id}/form-reviews/{request_id}"
        ).status_code, 404)

    def test_ashby_request_rejects_mismatched_route_identity(self):
        route_url = "https://jobs.ashbyhq.com/fixture/engineering-123"
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        for field, value in (("tenant", "other-tenant"),
                             ("requisition", "other-requisition")):
            with self.subTest(field=field):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.store.application(self.application_id)["route_id"],
                    ).values(
                        provider="ashby", tenant="fixture", requisition="engineering-123",
                        url=route_url,
                    ))
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.store.application(self.application_id)["route_id"],
                    ).values({field: value}))

                response = self.client.post(path, json={"request_id": f"mismatch-{field}"})

                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"],
                                 "FORM_REVIEW_ROUTE_UNSUPPORTED")
                with self.store.engine.connect() as connection:
                    queued = connection.execute(select(m.operations.c.id).where(
                        m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                        m.operations.c.entity_id == self.application_id,
                    )).all()
                    audits = connection.execute(select(m.audit.c.payload).where(
                        m.audit.c.entity_id == self.application_id,
                        m.audit.c.action == "APPLICATION_FORM_REVIEW",
                    )).all()
                self.assertEqual(queued, [])
                self.assertEqual(audits, [])

    def test_ashby_worker_rejects_route_identity_changed_after_queue(self):
        route_url = "https://jobs.ashbyhq.com/fixture/engineering-123"
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        for field, value in (("tenant", "other-tenant"),
                             ("requisition", "other-requisition")):
            with self.subTest(field=field):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.store.application(self.application_id)["route_id"],
                    ).values(
                        provider="ashby", tenant="fixture", requisition="engineering-123",
                        url=route_url,
                    ))
                request_id = f"worker-mismatch-{field}"
                queued = self.client.post(path, json={"request_id": request_id})
                self.assertEqual(queued.status_code, 202, queued.text)
                operation_id = queued.json()["operation_id"]
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.store.application(self.application_id)["route_id"],
                    ).values({field: value}))

                fetched = []

                class FakePublicClient:
                    def __init__(self, website, check=None, total_timeout=None):
                        self.website = website

                    def get(self, url):
                        fetched.append(url)
                        raise AssertionError("Mismatched route must not be fetched")

                    def allowed(self, url):
                        return url == route_url

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

                self.assertEqual(result["status"], "DEAD_LETTERED")
                self.assertEqual(fetched, [])
                with self.store.engine.connect() as connection:
                    operation = connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id,
                    )).mappings().one()
                    audits = connection.execute(select(m.audit.c.payload).where(
                        m.audit.c.entity_id == self.application_id,
                        m.audit.c.action == "APPLICATION_FORM_REVIEW",
                    )).all()
                self.assertEqual(operation["state"], "DEAD_LETTERED")
                self.assertEqual(audits, [])

    def test_ashby_public_route_queues_worker_and_reads_value_free_review(self):
        route_url = "https://jobs.ashbyhq.com/fixture/engineering-123"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(
                provider="ashby", tenant="fixture", requisition="engineering-123",
                url=route_url,
            ))

        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        request_id = "ashby-durable-review"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        html = (
            "<form>"
            "<label for='email'>Email address</label>"
            "<input id='email' name='email' type='email' required "
            "value='PRIVATE_ASHBY_EMAIL'>"
            "<label for='custom'>Describe your work authorization details</label>"
            "<input id='custom' name='custom' type='text' "
            "value='PRIVATE_ASHBY_ANSWER'>"
            "</form>"
        ).encode()
        fetched = []

        class FakeResponse:
            headers = {"content-type": "text/html; charset=utf-8"}
            content = html
            url = route_url

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check

            def get(self, url):
                self.check()
                fetched.append(url)
                if url != route_url:
                    raise AssertionError("Worker requested a different URL")
                return FakeResponse()

            def allowed(self, url):
                return url == route_url

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

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(fetched, [route_url])
        exact = self.client.get(path + "/" + request_id)
        latest = self.client.get(path + "/latest")
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(latest.status_code, 200, latest.text)
        expected_analysis = {
            "status": "REVIEW_REQUIRED",
            "reason": "FORM_REQUIRES_HUMAN_REVIEW",
            "fields": [
                {"id": "email", "name": "email", "type": "email", "required": True,
                 "review_reason": None},
                {"id": "custom", "name": "custom", "type": "text", "required": False,
                 "review_reason": "SENSITIVE_FIELD"},
            ],
        }
        self.assertEqual(exact.json()["state"], "COMPLETED")
        self.assertEqual(exact.json()["request_id"], request_id)
        self.assertEqual(exact.json()["analysis"], expected_analysis)
        self.assertEqual(latest.json()["state"], "COMPLETED")
        self.assertEqual(latest.json()["request_id"], request_id)
        self.assertEqual(latest.json()["analysis"], expected_analysis)

        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one()
            audits = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings())
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual(operation["params"]["provider"], "ashby")
        self.assertEqual(operation["params"]["route_url"], route_url)
        audit = next(row["payload"] for row in audits
                     if row["payload"].get("operation_id") == operation_id)
        self.assertEqual(audit["analysis"], expected_analysis)
        for private_value in ("PRIVATE_ASHBY_EMAIL", "PRIVATE_ASHBY_ANSWER"):
            self.assertNotIn(private_value, exact.text)
            self.assertNotIn(private_value, latest.text)
            self.assertNotIn(private_value, str(operation["params"]))
            self.assertNotIn(private_value, str(audit))

    def test_workday_public_route_queues_worker_and_reads_value_free_review(self):
        route_url = (
            "https://fixture.wd5.myworkdayjobs.com/en-US/Careers/job/"
            "Software_Engineer_R123"
        )
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(
                provider="workday", tenant="fixture", requisition="R123", url=route_url,
            ))

        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        request_id = "workday-public-review"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        html = (
            "<form>"
            "<label for='email'>Email</label>"
            "<input id='email' name='email' type='email' required "
            "value='PRIVATE_WORKDAY_EMAIL'>"
            "<label for='ssn'>Social Security Number</label>"
            "<input id='ssn' name='ssn' type='text' "
            "data-workday-conditional='false' data-workday-repeatable='false' "
            "value='PRIVATE_WORKDAY_SSN'>"
            "</form>"
        ).encode()
        fetched = []

        class FakeResponse:
            headers = {"content-type": "text/html; charset=utf-8"}
            content = html
            url = route_url

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check

            def get(self, url):
                self.check()
                fetched.append(url)
                if url != route_url:
                    raise AssertionError("Worker requested a different URL")
                return FakeResponse()

            def allowed(self, url):
                return url == route_url

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

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(fetched, [route_url])
        exact = self.client.get(path + "/" + request_id)
        latest = self.client.get(path + "/latest")
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(latest.status_code, 200, latest.text)
        expected_analysis = {
            "status": "REVIEW_REQUIRED",
            "reason": "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED",
            "fields": [
                {"id": "email", "name": "email", "type": "email", "required": True,
                 "review_reason": "UNKNOWN_QUESTION"},
                {"id": "ssn", "name": "ssn", "type": "text", "required": False,
                 "review_reason": "SENSITIVE_FIELD"},
            ],
        }
        self.assertEqual(exact.json()["state"], "COMPLETED")
        actual_analysis = exact.json()["analysis"]
        self.assertEqual(set(actual_analysis), {"status", "reason", "fields", "dynamic_review"})
        self.assertEqual({key: actual_analysis[key] for key in ("status", "reason", "fields")},
                         expected_analysis)
        from services.applications.workday_dynamic_review import validate_workday_dynamic_review
        envelope = validate_workday_dynamic_review(actual_analysis["dynamic_review"])
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope, actual_analysis["dynamic_review"])
        expected_analysis["dynamic_review"] = envelope
        self.assertEqual(exact.json()["analysis"], expected_analysis)
        self.assertEqual(latest.json()["analysis"], expected_analysis)

        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one()
            audits = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings())
        audit = next(row["payload"] for row in audits
                     if row["payload"].get("operation_id") == operation_id)
        self.assertEqual(audit["analysis"], expected_analysis)
        for private_value in ("PRIVATE_WORKDAY_EMAIL", "PRIVATE_WORKDAY_SSN"):
            self.assertNotIn(private_value, exact.text)
            self.assertNotIn(private_value, latest.text)
            self.assertNotIn(private_value, str(operation["params"]))
            self.assertNotIn(private_value, str(audit))

    def test_unsupported_or_malformed_requests_never_queue_a_fetch(self):
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        malformed = self.client.post(path, json={"request_id": "bad id", "submit": True})
        self.assertEqual(malformed.status_code, 422)
        wrong_profile = self.client.post(
            f"/profiles/missing/applications/{self.application_id}/form-reviews",
            json={"request_id": "review-request-002"},
        )
        self.assertEqual(wrong_profile.status_code, 404)


if __name__ == "__main__":
    unittest.main()
