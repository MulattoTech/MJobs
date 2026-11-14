"""Explicit preparation routes stay bound through durable worker completion."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.applications.preflight import latest_preflight, persist_preflight
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class ApplicationPrepareRouteBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-prepare-route-binding-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = "http://127.0.0.1:1"
        self.store = Store(f"sqlite:///{(self.root / 'routes.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.profile_version = self.store.profile("fixture")["version"]
        self.job_id = identity("prepare-route-binding", "job")
        self.company_id = identity("prepare-route-binding", "company")
        self.application_id = identity(self.job_id, "fixture")
        self.route_a = "prepare-route-a"
        self.route_b = "prepare-route-b"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=self.company_id, name="Synthetic Route Employer",
                domain="route-employer.example.invalid",
            ))
            connection.execute(m.jobs.insert().values(
                id=self.job_id, company_id=self.company_id, requisition="CANONICAL",
                title="Python Platform Engineer",
                description="Responsibilities: Maintain Python and SQL services.",
                skills=["Python", "SQL"], location="Remote", remote_type="REMOTE",
                attributes={},
            ))
            for route_id, requisition in (
                    (self.route_a, "ROUTE-A"), (self.route_b, "ROUTE-B")):
                connection.execute(m.routes.insert().values(
                    id=route_id, job_id=self.job_id, company_id=self.company_id,
                    provider="mock-ats", tenant="example", requisition=requisition,
                    url=f"{self.origin}/apply/{requisition}",
                ))
        self.environment = patch.dict(os.environ, {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_MOCK_ORIGIN": self.origin,
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_NOTIFICATIONS_ENABLED": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = TestClient(create_app(
            self.store, self.origin, self.root, operation_mode="FIXTURE",
        ))
        self.addCleanup(self.client.close)
        self.application_url = f"/profiles/fixture/applications/{self.application_id}"

    def operation(self, operation_id):
        response = self.client.get(f"/operations/{operation_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def application(self):
        response = self.client.get(self.application_url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def admit(self, route_id, request_id):
        response = self.client.post("/applications", json={
            "profile_id": "fixture", "job_id": self.job_id,
            "request_id": request_id, "expected_profile_version": self.profile_version,
            "route_mode": "THIS_ROUTE", "route_id": route_id,
        })
        self.assertEqual(response.status_code, 202, response.text)
        admitted = response.json()
        self.assertEqual(admitted["application_id"], self.application_id)
        self.assertEqual(admitted["route_ids"], [route_id])
        operation = self.operation(admitted["operation_id"])
        self.assertEqual(operation["kind"], "PREPARE")
        self.assertEqual(operation["state"], "QUEUED")
        self.assertEqual(operation["params"]["route_mode"], "THIS_ROUTE")
        self.assertEqual(operation["params"]["route_id"], route_id)
        return admitted["operation_id"]

    def execute(self, operation_id):
        with patch("workers.tasks.apply_mock", side_effect=AssertionError(
                "Document preparation must not launch an application adapter")) as adapter:
            result = execute_operation(operation_id)
        adapter.assert_not_called()
        return result

    def prepare_route_a(self):
        operation_id = self.admit(self.route_a, "initial-route-a")
        result = self.execute(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(self.operation(operation_id)["state"], "COMPLETED")
        application = self.application()
        self.assertEqual(application["state"], "PREPARED")
        self.assertEqual(application["route_id"], self.route_a)
        persist_preflight(self.store, self.application_id, {
            "result": "PASS", "reason": None, "observed_at": now(),
            "profile_version": application["profile_version"],
            "document_version": application["document_version"],
            "route_id": self.route_a, "provider": "mock-ats",
            "tenant": "example", "requisition": "ROUTE-A",
            "metadata_fingerprint": identity("prepare-route-binding", "preflight"),
            "mappings": [], "missing": [],
        })
        self.assertTrue(self.application()["question_preflight"]["is_current"])

    def lineage(self):
        with self.store.engine.connect() as connection:
            documents = [dict(row) for row in connection.execute(
                select(m.documents).where(
                    m.documents.c.application_id == self.application_id,
                ).order_by(m.documents.c.id),
            ).mappings()]
            choices = [dict(row) for row in connection.execute(
                select(m.application_route_choices).where(
                    m.application_route_choices.c.application_id == self.application_id,
                ).order_by(m.application_route_choices.c.id),
            ).mappings()]
            receipts = [dict(row) for row in connection.execute(
                select(m.audit).where(
                    m.audit.c.entity_id == self.application_id,
                    m.audit.c.action.in_((
                        "DOCUMENTS_PREPARED", "DOCUMENTS_VALIDATED",
                        "APPLICATION_QUESTION_PREFLIGHT",
                    )),
                ).order_by(m.audit.c.id),
            ).mappings()]
        artifacts = {}
        for document in documents:
            for format_name, relative in document["files"].items():
                path = self.root / self.application_id / relative
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(actual, document["sha256"][format_name])
                artifacts[f"{document['id']}:{format_name}"] = actual
        return {
            "application": self.store.application(self.application_id),
            "documents": documents, "choices": choices, "receipts": receipts,
            "preflight": latest_preflight(self.store, self.application_id),
            "artifacts": artifacts,
        }

    def insert_running_attempt(self, attempt_id):
        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(
                id=attempt_id, application_id=self.application_id,
                state="RUNNING", started_at=now(), steps=[],
            ))

    def assert_route_change_refused(self, request_id, code):
        before = self.lineage()
        operation_id = self.admit(self.route_b, request_id)
        result = self.execute(operation_id)
        self.assertEqual(result["status"], "DEAD_LETTERED", result)
        operation = self.operation(operation_id)
        self.assertEqual(operation["state"], "DEAD_LETTERED")
        self.assertEqual(operation["error"], code)
        self.assertEqual(self.lineage(), before)
        self.assertEqual(self.application()["route_id"], self.route_a)

    def test_explicit_route_change_rebinds_prepared_application_after_worker_completion(self):
        self.prepare_route_a()
        before = self.lineage()

        operation_id = self.admit(self.route_b, "explicit-route-b")
        result = self.execute(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(self.operation(operation_id)["state"], "COMPLETED")
        application = self.application()
        self.assertEqual(application["route_id"], self.route_b)
        self.assertEqual(application["profile_version"], self.profile_version)
        self.assertEqual(application["job_id"], self.job_id)
        self.assertEqual(application["state"], "PREPARED")
        self.assertEqual(
            application["document_version"],
            before["application"]["document_version"] + 1,
        )
        self.assertEqual(
            application["question_preflight"]["event_id"],
            before["preflight"]["event_id"],
        )
        self.assertFalse(application["question_preflight"]["is_current"])
        self.assertIn(self.route_b, {
            choice["route_id"] for choice in application["route_choices"]
        })

        after = self.lineage()
        documents_by_id = {row["id"]: row for row in after["documents"]}
        for document in before["documents"]:
            self.assertEqual(documents_by_id[document["id"]], document)
        for artifact, digest in before["artifacts"].items():
            self.assertEqual(after["artifacts"][artifact], digest)
        receipts_by_id = {row["id"]: row for row in after["receipts"]}
        for receipt in before["receipts"]:
            self.assertEqual(receipts_by_id[receipt["id"]], receipt)
        prepared = [
            row["payload"] for row in after["receipts"]
            if row["action"] == "DOCUMENTS_PREPARED"
            and row["payload"]["document_version"] == application["document_version"]
        ]
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]["route_ids"], [self.route_b])
        self.assertEqual(prepared[0]["preparation_config"]["route_mode"], "THIS_ROUTE")
        self.assertEqual(prepared[0]["preparation_config"]["route_id"], self.route_b)
        self.assertEqual(
            prepared[0]["preparation_config"]["resolved_route_id"], self.route_b,
        )

    def test_same_route_reuse_preserves_lineage_even_with_a_running_attempt(self):
        self.prepare_route_a()
        for active_attempt in (False, True):
            with self.subTest(active_attempt=active_attempt):
                if active_attempt:
                    self.insert_running_attempt("same-route-running-attempt")
                before = self.lineage()
                attempts_before = self.application()["attempts"]
                operation_id = self.admit(
                    self.route_a, f"same-route-reuse-{active_attempt}",
                )
                result = self.execute(operation_id)
                self.assertEqual(result["status"], "COMPLETED", result)
                self.assertEqual(self.operation(operation_id)["state"], "COMPLETED")
                self.assertEqual(self.lineage(), before)
                self.assertEqual(self.application()["attempts"], attempts_before)

    def test_route_change_preserves_locked_application_history(self):
        self.prepare_route_a()
        for state in ("CONFIRMED", "SUBMITTING", "SUBMISSION_UNCONFIRMED"):
            with self.subTest(state=state):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.applications).where(
                        m.applications.c.id == self.application_id,
                    ).values(state=state))
                self.assert_route_change_refused(
                    f"locked-route-change-{state}", "DOCUMENT_HISTORY_LOCKED",
                )

    def test_route_change_is_refused_while_an_application_attempt_is_running(self):
        self.prepare_route_a()
        self.insert_running_attempt("route-change-running-attempt")
        attempts_before = self.application()["attempts"]
        self.assert_route_change_refused(
            "route-change-during-attempt", "APPLICATION_REPREPARE_IN_PROGRESS",
        )
        self.assertEqual(self.application()["attempts"], attempts_before)

    def test_route_change_is_refused_while_application_work_owns_the_route(self):
        self.prepare_route_a()
        activities = (
            ("APPLY_MOCK", "QUEUED", None),
            ("APPLY_MOCK", "RUNNING", None),
            ("MANUAL_AUTH", "QUEUED", None),
            ("MANUAL_AUTH", "RUNNING", None),
            ("MANUAL_AUTH", "BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"),
            ("APPLICATION_FORM_REVIEW", "RUNNING", None),
            ("APPLICATION_DRY_RUN", "RUNNING", None),
        )
        for index, (kind, state, error) in enumerate(activities):
            with self.subTest(kind=kind, state=state, error=error):
                activity_id = f"route-owner-{index}"
                with self.store.engine.begin() as connection:
                    connection.execute(m.operations.insert().values(
                        id=activity_id, kind=kind, entity_id=self.application_id,
                        params={"fixture_activity": True}, state=state, error=error,
                        updated_at=now(),
                        lease_expires_at=(
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat() if state == "RUNNING" else None,
                    ))
                    activity_before = dict(connection.execute(
                        select(m.operations).where(m.operations.c.id == activity_id),
                    ).mappings().one())
                try:
                    self.assert_route_change_refused(
                        f"route-change-during-owner-{index}",
                        "APPLICATION_REPREPARE_IN_PROGRESS",
                    )
                    with self.store.engine.connect() as connection:
                        activity_after = dict(connection.execute(
                            select(m.operations).where(m.operations.c.id == activity_id),
                        ).mappings().one())
                    self.assertEqual(activity_after, activity_before)
                finally:
                    with self.store.engine.begin() as connection:
                        connection.execute(m.operations.delete().where(
                            m.operations.c.id == activity_id,
                        ))


    def test_route_change_ignores_other_applications_and_released_work(self):
        self.prepare_route_a()
        original = self.application()
        other_profile = deepcopy(PROFILE)
        other_profile.update(
            id="other-route-profile", email="other-route@example.invalid",
        )
        self.store.save_profile(other_profile)
        other_application_id = identity(self.job_id, other_profile["id"])
        response = self.client.post("/applications", json={
            "profile_id": other_profile["id"], "job_id": self.job_id,
            "request_id": "prepare-other-route-owner",
            "expected_profile_version": self.store.profile(other_profile["id"])["version"],
            "route_mode": "THIS_ROUTE", "route_id": self.route_a,
        })
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["application_id"], other_application_id)
        prepared = self.execute(response.json()["operation_id"])
        self.assertEqual(prepared["status"], "COMPLETED", prepared)

        other_activity = (
            ("APPLY_MOCK", "QUEUED", None),
            ("APPLY_MOCK", "RUNNING", None),
            ("MANUAL_AUTH", "QUEUED", None),
            ("MANUAL_AUTH", "RUNNING", None),
            ("MANUAL_AUTH", "BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"),
            ("APPLICATION_FORM_REVIEW", "RUNNING", None),
            ("APPLICATION_DRY_RUN", "RUNNING", None),
        )
        released_activity = (
            ("APPLY_MOCK", "COMPLETED", None),
            ("APPLY_MOCK", "CANCELLED", None),
            ("MANUAL_AUTH", "COMPLETED", None),
            ("MANUAL_AUTH", "CANCELLED", None),
            ("MANUAL_AUTH", "BLOCKED", "SYNTHETIC_REVIEW_REQUIRED"),
            ("APPLICATION_FORM_REVIEW", "COMPLETED", None),
            ("APPLICATION_DRY_RUN", "COMPLETED", None),
        )
        owner_ids = []
        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(
                id="other-application-running-attempt",
                application_id=other_application_id,
                state="RUNNING", started_at=now(), steps=[],
            ))
            for owner_application, activities in (
                    (other_application_id, other_activity),
                    (self.application_id, released_activity)):
                for index, (kind, state, error) in enumerate(activities):
                    owner_id = identity("route-scope-owner", owner_application, str(index))
                    owner_ids.append(owner_id)
                    connection.execute(m.operations.insert().values(
                        id=owner_id, kind=kind, entity_id=owner_application,
                        params={"fixture_activity": True}, state=state, error=error,
                        updated_at=now(),
                        lease_expires_at=(
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat() if state == "RUNNING" else None,
                    ))
            owners_before = [dict(row) for row in connection.execute(
                select(m.operations).where(
                    m.operations.c.id.in_(owner_ids),
                ).order_by(m.operations.c.id),
            ).mappings()]
        other_url = (
            f"/profiles/{other_profile['id']}/applications/{other_application_id}"
        )
        other_before = self.client.get(other_url)
        self.assertEqual(other_before.status_code, 200, other_before.text)

        operation_id = self.admit(self.route_b, "route-change-with-nonowners")
        result = self.execute(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(self.operation(operation_id)["state"], "COMPLETED")
        application = self.application()
        self.assertEqual(application["route_id"], self.route_b)
        self.assertEqual(
            application["document_version"], original["document_version"] + 1,
        )
        self.assertEqual(application["profile_version"], original["profile_version"])
        self.assertFalse(application["question_preflight"]["is_current"])
        with self.store.engine.connect() as connection:
            owners_after = [dict(row) for row in connection.execute(
                select(m.operations).where(
                    m.operations.c.id.in_(owner_ids),
                ).order_by(m.operations.c.id),
            ).mappings()]
        self.assertEqual(owners_after, owners_before)
        other_after = self.client.get(other_url)
        self.assertEqual(other_after.status_code, 200, other_after.text)
        self.assertEqual(other_after.json(), other_before.json())

if __name__ == "__main__":
    unittest.main()
