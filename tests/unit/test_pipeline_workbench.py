from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import update

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from tests.integration.test_spine import PROFILE


class PipelineWorkbenchTests(unittest.TestCase):
    def test_profile_wide_rescore_blocks_preparation_and_recovers_saved_status(self):
        from workers.dispatch import enqueue_operation
        self.add_job("scoring-job")
        before = self.read()["items"][0]
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", {
            "rescore_only": True, "profile_version": self.profile_version,
            "request_id": "refresh-profile-scores",
        })
        snapshot = self.read()
        item = snapshot["items"][0]
        self.assertFalse(item["can_prepare"])
        self.assertIn("DISCOVERY_RUNNING", {b["code"] for b in item["blockers"]})
        self.assertNotEqual(item["fingerprint"], before["fingerprint"])
        self.assertEqual(snapshot["scoring_operation"], {"operation_id": operation_id, "state": "QUEUED"})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(state="COMPLETED"))
        recovered = self.read()
        self.assertTrue(recovered["items"][0]["can_prepare"])
        self.assertEqual(recovered["scoring_operation"]["state"], "COMPLETED")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'pipeline.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile = deepcopy(PROFILE)
        self.store.save_profile(self.profile)
        self.profile_version = self.store.profile("fixture")["version"]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def add_job(self, job_id, *, score=85, score_version=None, route_provider="greenhouse",
                application=False, docs=(), application_state="PREPARED",
                route_review_required=False):
        company_id = f"company-{job_id}"
        route_id = f"route-{job_id}"
        application_id = f"application-{job_id}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name=f"Company {job_id}", domain=f"{job_id}.example.test"))
            attributes = ({"source_resolution": {"route_review_required": True,
                                                    "preserved_route_id": "other-route"}}
                          if route_review_required else {})
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=company_id, requisition=f"REQ-{job_id}",
                title=f"Engineer {job_id}", description="Synthetic role description",
                skills=["Python"], location="Remote", remote_type="REMOTE",
                attributes=attributes))
            if score is not None:
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id, profile_id="fixture",
                    profile_version=score_version or self.profile_version,
                    score=score, missing=[], components={"skills": 90}))
            if route_provider is not None:
                connection.execute(m.routes.insert().values(
                    id=route_id, job_id=job_id, company_id=company_id,
                    provider=route_provider, tenant=f"tenant-{job_id}",
                    requisition=f"REQ-{job_id}",
                    url=f"https://jobs.example.test/{job_id}?token=private"))
            if application:
                connection.execute(m.applications.insert().values(
                    id=application_id, job_id=job_id, route_id=route_id,
                    profile_id="fixture", profile_version=self.profile_version,
                    state=application_state, target="MOCK", fact_ids=["python"],
                    trace={"private_answer": "do-not-return"}, document_version=1,
                    confirmation={"receipt": "private"},
                    created_at="2026-10-11T12:00:00Z", submitted_at=None))
                for index, (kind, version, doc_profile_version) in enumerate(docs):
                    connection.execute(m.documents.insert().values(
                        id=f"doc-{job_id}-{kind}-{index}", application_id=application_id,
                        kind=kind, version=version, profile_version=doc_profile_version,
                        files={kind: "private/path"}, fact_ids=["python"],
                        sha256={kind: "a" * 64}, template="safe-test",
                        created_at="2026-10-11T12:00:00Z", generation_context={}))
        return {"job_id": job_id, "company_id": company_id, "route_id": route_id,
                "application_id": application_id if application else None}

    def read(self, limit=100):
        try:
            from services.operations.pipeline_workbench import read_pipeline_workbench
        except ImportError as error:
            self.fail(f"pipeline readiness projection is missing: {error}")
        return read_pipeline_workbench(self.store, "fixture", limit=limit)

    def test_current_scored_job_with_route_is_actionable_without_inventing_application(self):
        self.add_job("ready-job")
        result = self.read()
        self.assertEqual(result["profile_id"], "fixture")
        self.assertEqual(result["profile_version"], self.profile_version)
        self.assertEqual(result["counts"]["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["stage"], "READY_TO_PREPARE")
        self.assertTrue(item["can_prepare"])
        self.assertEqual(item["score"], 85)
        self.assertIsNone(item["application_id"])
        self.assertEqual(item["route_id"], "route-ready-job")
        self.assertEqual(item["next_action"], "PREPARE")
        self.assertIn("DOCUMENTS_MISSING", {blocker["code"] for blocker in item["blockers"]})
        self.assertEqual(len(item["fingerprint"]), 64)
        self.assertNotIn("employer_confirmation", item)
        self.assertNotIn("private", repr(item))
        self.assertNotIn("example.test", repr(item))

    def test_stale_profile_score_and_documents_are_not_reported_as_current(self):
        job = self.add_job(
            "stale-job", application=True,
            docs=(("resume", 1, self.profile_version), ("cover-letter", 1, self.profile_version)))
        changed = deepcopy(self.profile)
        changed["facts"][0]["text"] = "Updated synthetic approved fact"
        self.store.save_profile(changed, expected_version=self.profile_version)

        item = next(item for item in self.read()["items"] if item["job_id"] == job["job_id"])
        self.assertEqual(item["stage"], "NEEDS_SCORING")
        self.assertFalse(item["can_prepare"])
        self.assertIsNone(item["score"])
        self.assertEqual(item["documents_state"], "STALE")
        self.assertEqual(item["score_state"], "STALE")
        self.assertTrue({"SCORE_STALE", "DOCUMENTS_STALE"}.issubset(
            {blocker["code"] for blocker in item["blockers"]}))

    def test_current_match_wins_over_lexically_later_historical_profile_version(self):
        self.add_job("version-order-job", score=87)
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.insert().values(
                id="historical-lexically-later", job_id="version-order-job",
                profile_id="fixture", profile_version="z" * 64, score=11,
                missing=[], components={"skills": 10}))
        item = self.read()["items"][0]
        self.assertEqual(item["score_state"], "CURRENT")
        self.assertEqual(item["score"], 87)
        self.assertEqual(item["stage"], "READY_TO_PREPARE")

    def test_current_documents_are_prepared_but_only_explicit_user_tracking_enters_tracking(self):
        job = self.add_job("prepared-job", application=True, application_state="CONFIRMED",
                           docs=(("resume", 1, self.profile_version),
                                 ("cover-letter", 1, self.profile_version)))
        prepared = next(item for item in self.read()["items"] if item["job_id"] == job["job_id"])
        self.assertEqual(prepared["stage"], "PREPARED")
        self.assertEqual(prepared["documents_state"], "CURRENT")
        self.assertIsNone(prepared["tracking_status"])
        self.assertNotIn("employer_confirmation", prepared)

        entity = identity("application-tracking", "fixture", job["application_id"])
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id="user-reported-interview", entity_id=entity,
                action="APPLICATION_PIPELINE_STATUS",
                payload={"status": "INTERVIEW", "attribution": "USER_REPORTED", "sequence": 1},
                timestamp="2026-10-11T13:00:00Z"))
        tracked = next(item for item in self.read()["items"] if item["job_id"] == job["job_id"])
        self.assertEqual(tracked["stage"], "TRACKING")
        self.assertEqual(tracked["tracking_status"], "INTERVIEW")
        self.assertEqual(tracked["tracking_attribution"], "USER_REPORTED")

    def test_email_reported_status_is_preserved_as_user_tracking(self):
        job = self.add_job("email-tracking-job", application=True,
                           docs=(("resume", 1, self.profile_version),
                                 ("cover-letter", 1, self.profile_version)))
        entity = identity("application-tracking", "fixture", job["application_id"])
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id="email-reported-applied", entity_id=entity,
                action="APPLICATION_PIPELINE_STATUS",
                payload={"status": "APPLIED", "attribution": "USER_REPORTED_EMAIL",
                         "sequence": 1},
                timestamp="2026-10-11T14:00:00Z"))
        item = self.read()["items"][0]
        self.assertEqual(item["stage"], "TRACKING")
        self.assertEqual(item["tracking_status"], "APPLIED")
        self.assertEqual(item["tracking_attribution"], "USER_REPORTED_EMAIL")

    def test_active_prepare_operation_and_personal_mock_route_are_hard_blockers(self):
        self.add_job("active-job")
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id="active-prepare", kind="PREPARE", entity_id="fixture",
                params={"job_id": "active-job", "profile_version": self.profile_version},
                state="RUNNING", error=None, updated_at="2026-10-11T13:00:00Z"))
        active = next(item for item in self.read()["items"] if item["job_id"] == "active-job")
        self.assertEqual(active["stage"], "PREPARING")
        self.assertFalse(active["can_prepare"])
        self.assertEqual(active["operation_id"], "active-prepare")
        self.assertEqual(active["next_action"], "WAIT_FOR_OPERATION")

        mock_job = self.add_job("mock-job", route_provider="mock-ats")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.workspace_identity).where(
                m.workspace_identity.c.id == "workspace").values(operation_mode="PERSONAL"))
        mock = next(item for item in self.read()["items"] if item["job_id"] == mock_job["job_id"])
        self.assertFalse(mock["can_prepare"])
        self.assertIn("MOCK_ROUTE_IN_PERSONAL_MODE",
                      {blocker["code"] for blocker in mock["blockers"]})

    def test_unbound_workspace_mode_cannot_authorize_preparation(self):
        self.add_job("unbound-job")
        with self.store.engine.begin() as connection:
            connection.execute(m.workspace_identity.delete().where(
                m.workspace_identity.c.id == "workspace"))
        item = self.read()["items"][0]
        self.assertFalse(item["can_prepare"])
        self.assertEqual(item["operation_mode"], "UNBOUND")
        self.assertIn("WORKSPACE_MODE_UNAVAILABLE",
                      {blocker["code"] for blocker in item["blockers"]})

    def test_scoped_union_is_stable_bounded_and_returns_empty_state(self):
        self.add_job("z-job")
        self.add_job("a-job")
        first = self.read(limit=1)
        self.assertEqual(first, self.read(limit=1))
        self.assertEqual(first["counts"]["total"], 1)
        self.assertTrue(first.get("truncated"))
        self.assertEqual(first["items"][0]["job_id"], "a-job")

        self.store.save_profile({**deepcopy(PROFILE), "id": "empty", "email": "empty@example.test"})
        try:
            from services.operations.pipeline_workbench import read_pipeline_workbench
        except ImportError as error:
            self.fail(f"pipeline readiness projection is missing: {error}")
        self.assertEqual(read_pipeline_workbench(self.store, "empty"), {
            "profile_id": "empty", "profile_version": self.store.profile("empty")["version"],
            "items": [], "scoring_operation": None, "counts": {"total": 0, "by_stage": {
                "DISCOVERED": 0, "NEEDS_SCORING": 0, "NEEDS_REVIEW": 0,
                "READY_TO_PREPARE": 0, "PREPARING": 0, "PREPARED": 0, "TRACKING": 0,
            }}, "truncated": False,
        })
        with self.assertRaisesRegex(ValueError, "PIPELINE_LIMIT_INVALID"):
            read_pipeline_workbench(self.store, "fixture", limit=0)
        with self.assertRaisesRegex(ValueError, "PIPELINE_LIMIT_INVALID"):
            read_pipeline_workbench(self.store, "fixture", limit=1000)


if __name__ == "__main__":
    unittest.main()
