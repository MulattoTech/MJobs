"""Per-profile discovery policy controls durable company-discovery intake."""
import copy
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy import delete, event, select, update

from packages.domain.profile import JobPreferences
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.requests import (
    approve_discovery_request,
    record_discovery_request,
)
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.dispatch import _pending_operations
from workers.operations import claim


class CompanyDiscoveryPolicyTests(unittest.TestCase):
    def test_profile_preferences_have_safe_defaults_and_validate_thresholds(self):
        defaults = JobPreferences().model_dump()["company_discovery"]
        self.assertEqual(defaults, {
            "company_discovery_enabled": True,
            "minimum_match_for_discovery": 60,
            "priority_match_for_discovery": 80,
            "max_new_company_discoveries_per_hour": 12,
        })
        configured = JobPreferences.model_validate({"company_discovery": {
            "company_discovery_enabled": False,
            "minimum_match_for_discovery": 72,
            "priority_match_for_discovery": 91,
            "max_new_company_discoveries_per_hour": 4,
        }})
        self.assertEqual(configured.company_discovery.minimum_match_for_discovery, 72)
        for invalid in (
            {"minimum_match_for_discovery": 91, "priority_match_for_discovery": 90},
            {"minimum_match_for_discovery": 101},
            {"max_new_company_discoveries_per_hour": 0},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                JobPreferences.model_validate({"company_discovery": invalid})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-company-discovery-policy-")
        self.root = Path(self.temp.name)
        self.store = self._new_store()
        self.store.bind_workspace_mode("PERSONAL")
        self.source = configure_source(self.store, "policy-example")
        self.save_profile()

    def _new_store(self):
        store = Store(f"sqlite:///{(self.root / 'policy.db').as_posix()}")
        store.migrate()
        return store

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def save_profile(self, **policy):
        profile = copy.deepcopy(PROFILE)
        profile["preferences"] = {
            **profile.get("preferences", {}),
            "company_discovery": {
                "company_discovery_enabled": True,
                "minimum_match_for_discovery": 60,
                "priority_match_for_discovery": 80,
                "max_new_company_discoveries_per_hour": 12,
                **policy,
            },
        }
        self.store.save_profile(profile)
        return self.store.profile("fixture")

    def collect(self, source=None, raw=None):
        def response(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": [raw or RAW]})
            tenant = request.url.path.split("/")[3]
            return httpx.Response(200, json={"name": f"Example Employer {tenant}"})
        return collect_source(
            self.store, source or self.source, "fixture",
            transport=httpx.MockTransport(response),
        )

    def test_disabled_policy_prevents_new_profile_discovery_requests(self):
        self.save_profile(company_discovery_enabled=False)
        self.collect()
        self.assertEqual(self.store.count("company_discovery_requests"), 0)
        persisted = self.store.profile("fixture")
        self.assertFalse(persisted["preferences"]["company_discovery"]["company_discovery_enabled"])

    def test_thresholds_control_high_priority_and_rate_limit_new_requests(self):
        self.save_profile(
            minimum_match_for_discovery=70,
            priority_match_for_discovery=90,
            max_new_company_discoveries_per_hour=1,
        )
        self.collect()
        with self.store.engine.begin() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.profile_id == "fixture")).mappings().one())
            job_id = request["trigger_job_id"]
            connection.execute(delete(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request["id"]))
            connection.execute(update(m.matches).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == "fixture",
            ).values(score=85))
            new_id = record_discovery_request(connection, job_id, self.store.profile("fixture"))
            self.assertIsNotNone(new_id)
            below_priority = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == new_id)).mappings().one())
            self.assertNotIn("HIGH_MATCH_JOB", below_priority["reasons"])
            self.assertIn("MATCH_ELIGIBLE_JOB", below_priority["reasons"])

        self.assertEqual(self.store.count("company_discovery_requests"), 1)
        # A second, independently recognized employer is held by the hourly cap.
        second_source = configure_source(self.store, "policy-second")
        second_raw = dict(RAW, id=9876, internal_job_id=9876,
                          absolute_url="https://job-boards.greenhouse.io/policy-second/jobs/9876")
        self.collect(second_source, second_raw)
        self.assertEqual(self.store.count("company_discovery_requests"), 1)

    def test_high_match_threshold_is_configurable_and_dispatch_priority_survives_review(self):
        self.save_profile(minimum_match_for_discovery=70, priority_match_for_discovery=90)
        self.collect()
        with self.store.engine.begin() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.profile_id == "fixture")).mappings().one())
            connection.execute(update(m.matches).where(
                m.matches.c.job_id == request["trigger_job_id"],
                m.matches.c.profile_id == "fixture",
            ).values(score=89))
            connection.execute(delete(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request["id"]))
            below_priority_id = record_discovery_request(
                connection, request["trigger_job_id"], self.store.profile("fixture"),
                is_new_company=False,
            )
            below_priority = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == below_priority_id)).mappings().one())
            self.assertIn("MATCH_ELIGIBLE_JOB", below_priority["reasons"])
            self.assertNotIn("HIGH_MATCH_JOB", below_priority["reasons"])
            connection.execute(delete(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == below_priority_id))
            connection.execute(update(m.matches).where(
                m.matches.c.job_id == request["trigger_job_id"],
                m.matches.c.profile_id == "fixture",
            ).values(score=90))
            request_id = record_discovery_request(
                connection, request["trigger_job_id"], self.store.profile("fixture"),
                is_new_company=False,
            )
            queued_request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request_id)).mappings().one())
        self.assertIn("HIGH_MATCH_JOB", queued_request["reasons"])
        from services.company_discovery.domains import record_company_domain
        record_company_domain(self.store, request["company_id"], "employer.example",
                              "VERIFIED_EXISTING_DOMAIN", ["Synthetic reviewed evidence"])
        # A reviewed domain allows discovery to queue, but never enables submission.
        with self.store.engine.begin() as connection:
            current = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request_id)).mappings().one())
        receipt = approve_discovery_request(
            self.store, request_id, "https://employer.example/", current["revision"],
        )
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == receipt["operation_id"])).mappings().one()
        self.assertEqual(operation["params"]["dispatch_priority"], "HIGH")
        self.assertEqual(operation["params"]["fetch_mode"], "PUBLIC_HTTPS")

    def test_outbox_dispatches_high_priority_work_before_older_normal_work(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert(), [
                {"id": "normal-company", "kind": "COMPANY_DISCOVER", "entity_id": "normal",
                 "params": {"dispatch_priority": "NORMAL"}, "state": "QUEUED", "error": None,
                 "updated_at": "2026-10-09T10:00:00+00:00"},
                {"id": "high-company", "kind": "COMPANY_DISCOVER", "entity_id": "high",
                 "params": {"dispatch_priority": "HIGH"}, "state": "QUEUED", "error": None,
                 "updated_at": "2026-10-09T11:00:00+00:00"},
                {"id": "other-kind", "kind": "PREPARE", "entity_id": "profile",
                 "params": {}, "state": "QUEUED", "error": None,
                 "updated_at": "2026-10-09T09:00:00+00:00"},
            ])
            ids = [row["id"] for row in _pending_operations(connection)]
        self.assertEqual(ids, ["high-company", "other-kind", "normal-company"])

    def _queued_normal_request(self, *, verified=True):
        """Import a real fixture, then authorize discovery below its priority threshold."""
        self.save_profile(minimum_match_for_discovery=70, priority_match_for_discovery=90)
        self.collect()
        self.profile = self.store.profile("fixture")
        with self.store.engine.begin() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            self.assertIsNone(request["operation_id"])
            connection.execute(delete(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request["id"]))
            connection.execute(update(m.matches).where(
                m.matches.c.job_id == request["trigger_job_id"],
                m.matches.c.profile_id == self.profile["id"],
                m.matches.c.profile_version == self.profile["version"],
            ).values(score=89))
            record_discovery_request(connection, request["trigger_job_id"], self.profile)
            request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
        if verified:
            from services.company_discovery.domains import record_company_domain
            record_company_domain(self.store, request["company_id"], "employer.example",
                                  "VERIFIED_EXISTING_DOMAIN", ["Synthetic reviewed evidence"])
        receipt = approve_discovery_request(
            self.store, request["id"], "https://employer.example/", request["revision"],
            confirm_company_identity=verified,
        )
        with self.store.engine.connect() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == receipt["operation_id"])).mappings().one())
        self.assertEqual(operation["params"]["dispatch_priority"], "NORMAL")
        return request, operation

    def _record_score(self, connection, request, score=90):
        connection.execute(update(m.matches).where(
            m.matches.c.job_id == request["trigger_job_id"],
            m.matches.c.profile_id == self.profile["id"],
            m.matches.c.profile_version == self.profile["version"],
        ).values(score=score))
        return record_discovery_request(connection, request["trigger_job_id"], self.profile)

    def test_queued_discovery_is_promoted_once_without_replacing_authority_or_retry_state(self):
        request, operation = self._queued_normal_request()
        # Existing retry information is scheduling authority, not reset by a rescore.
        retry_state = {"attempt_count": 2, "retry_count": 1,
                           "next_attempt_at": "2026-01-01T10:00:00+00:00",
                           "recovery_metadata": {"synthetic": "retained"}}
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation["id"])
                               .values(**retry_state))
            connection.execute(m.operations.insert().values(
                id="older-normal", kind="COMPANY_DISCOVER", entity_id="other-company",
                params={"dispatch_priority": "NORMAL"}, state="QUEUED",
                updated_at="2026-01-01T09:00:00+00:00"))
            self._record_score(connection, request, 89)
            unchanged = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation["id"])).mappings().one())
            self.assertEqual(unchanged, {**operation, **retry_state})
            self.assertEqual(self._record_score(connection, request), request["id"])
            # Repeated collection/rescoring cannot enqueue duplicate work or audit records.
            self._record_score(connection, request, 95)
        with self.store.engine.connect() as connection:
            actual = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation["id"])).mappings().one())
            current = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            ids = [row["id"] for row in _pending_operations(connection)]
            audits = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == request["id"],
                m.audit.c.action == "COMPANY_DISCOVERY_REQUEST_PRIORITY_PROMOTED")).scalars().all()
        self.assertEqual(actual, {**operation, **retry_state,
                                  "params": {**operation["params"], "dispatch_priority": "HIGH"}})
        self.assertEqual(current["operation_id"], operation["id"])
        self.assertEqual(current["revision"], request["revision"])
        self.assertEqual(current["highest_match_score"], 95)
        self.assertIn("HIGH_MATCH_JOB", current["reasons"])
        self.assertEqual(ids, [operation["id"], "older-normal"])
        self.assertEqual(audits, [{"revision": request["revision"],
                                 "operation_id": operation["id"],
                                 "previous_priority": "NORMAL", "dispatch_priority": "HIGH"}])
        self.assertEqual(self.store.count("worker_operations"), 2)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_unverified_review_candidate_keeps_its_distinct_identity_when_promoted(self):
        request, operation = self._queued_normal_request(verified=False)
        self.assertEqual(request["authorization"], "USER_CANDIDATE")
        self.assertNotEqual(operation["entity_id"], request["company_id"])
        with self.store.engine.begin() as connection:
            self._record_score(connection, request)
        with self.store.engine.connect() as connection:
            actual = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation["id"])).mappings().one())
        self.assertEqual(actual, {**operation, "params": {
            **operation["params"], "dispatch_priority": "HIGH"}})
        self.assertEqual(self.store.count("company_domains"), 0)

    def test_old_normal_delivery_defers_to_promoted_work_without_consuming_attempt(self):
        request, operation = self._queued_normal_request()
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id="old-normal-delivery", kind="COMPANY_DISCOVER", entity_id="other-company",
                params={"dispatch_priority": "NORMAL"}, state="QUEUED",
                updated_at="2026-01-01T09:00:00+00:00"))
            before = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == "old-normal-delivery")).mappings().one())
            self._record_score(connection, request)
        # RQ may already contain the normal operation before the promoted task.
        self.assertIsNone(claim(self.store, "old-normal-delivery"))
        with self.store.engine.connect() as connection:
            self.assertEqual(dict(connection.execute(select(m.operations).where(
                m.operations.c.id == "old-normal-delivery")).mappings().one()), before)
            self.assertFalse(connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == "old-normal-delivery")).first())
        claimed = claim(self.store, operation["id"])
        self.assertEqual(claimed["params"]["dispatch_priority"], "HIGH")
        self.assertEqual(claimed["attempt_count"], 1)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation["id"])
                               .values(state="COMPLETED"))
        self.assertEqual(claim(self.store, "old-normal-delivery")["attempt_count"], 1)

    def test_cancelled_backoff_terminal_or_other_category_high_work_does_not_block_claim(self):
        cases = [{"cancel_requested": True}, {"next_attempt_at": "2099-01-01T00:00:00+00:00"},
                 {"state": "COMPLETED"}, {"kind": "PREPARE"}]
        for index, values in enumerate(cases):
            with self.subTest(values=values):
                normal_id, high_id = f"normal-{index}", f"high-{index}"
                with self.store.engine.begin() as connection:
                    connection.execute(m.operations.insert().values(
                        id=normal_id, kind="COMPANY_DISCOVER", entity_id="other-company",
                        params={"dispatch_priority": "NORMAL"}, state="QUEUED",
                        updated_at="2026-01-01T09:00:00+00:00"))
                    connection.execute(m.operations.insert().values(**{
                        "id": high_id, "kind": "COMPANY_DISCOVER", "entity_id": "high-company",
                        "params": {"dispatch_priority": "HIGH"}, "state": "QUEUED",
                        "updated_at": "2026-01-01T09:00:00+00:00", **values}))
                    high_before = dict(connection.execute(select(m.operations).where(
                        m.operations.c.id == high_id)).mappings().one())
                self.assertEqual(claim(self.store, normal_id)["attempt_count"], 1)
                with self.store.engine.begin() as connection:
                    self.assertEqual(dict(connection.execute(select(m.operations).where(
                        m.operations.c.id == high_id)).mappings().one()), high_before)
                    connection.execute(update(m.operations).where(m.operations.c.id == normal_id)
                                       .values(state="COMPLETED"))

    def test_promoted_retry_keeps_future_deadline_and_revoked_source_still_blocks_authorization(self):
        request, operation = self._queued_normal_request()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation["id"])
                               .values(next_attempt_at="2099-01-01T00:00:00+00:00", retry_count=1))
            self._record_score(connection, request)
        self.assertIsNone(claim(self.store, operation["id"]))
        from services.company_discovery.public_client import PublicFetchError
        from services.company_discovery.requests import authorize_discovery_request
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source)
                               .values(enabled=False))
            promoted = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation["id"])).mappings().one())
        self.assertEqual(promoted["params"]["dispatch_priority"], "HIGH")
        self.assertEqual(promoted["next_attempt_at"], "2099-01-01T00:00:00+00:00")
        self.assertEqual(promoted["retry_count"], 1)
        self.assertEqual(promoted["attempt_count"], 0)
        with self.assertRaisesRegex(PublicFetchError, "SOURCE_DISABLED"):
            authorize_discovery_request(self.store, promoted)

    def test_promotion_rolls_back_with_match_and_request_transaction(self):
        request, operation = self._queued_normal_request()
        with (self.assertRaisesRegex(RuntimeError, "synthetic rollback"),
              self.store.engine.begin() as connection):
            self._record_score(connection, request)
            raise RuntimeError("synthetic rollback")
        with self.store.engine.connect() as connection:
            self.assertEqual(dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation["id"])).mappings().one()), operation)
            self.assertEqual(dict(connection.execute(select(m.company_discovery_requests))
                                  .mappings().one()), request)
            self.assertEqual(connection.execute(select(m.matches.c.score).where(
                m.matches.c.job_id == request["trigger_job_id"],
                m.matches.c.profile_version == self.profile["version"],
            )).scalar_one(), 89)
            self.assertFalse(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action == "COMPANY_DISCOVERY_REQUEST_PRIORITY_PROMOTED")).first())

    def test_rescore_does_not_promote_claimed_terminal_cancelled_or_foreign_work(self):
        request, operation = self._queued_normal_request()
        params = operation["params"]
        cases = [{"state": state} for state in
                 ("RUNNING", "COMPLETED", "FAILED", "BLOCKED", "CANCELLED")]
        cases += [{"cancel_requested": True}, {"kind": "DISCOVER"}, {"entity_id": "other"},
                  {"params": {**params, "discovery_request_id": "other"}},
                  {"params": {**params, "discovery_request_revision": request["revision"] + 1}},
                  {"params": {**params, "profile_id": "other"}},
                  {"params": {**params, "profile_version": self.profile["version"] + "-stale"}},
                  {"params": {**params, "candidate": {**params["candidate"],
                                                      "company_website": "https://other.example/"}}},
                  {"params": {**params, "candidate": None}}, {"params": None}]
        # JSON null is a persisted malformed payload; it is not new discovery authority.
        for values in cases:
            with self.subTest(values=values), self.store.engine.begin() as connection:
                before = {**operation, **values}
                connection.execute(update(m.operations).where(m.operations.c.id == operation["id"])
                                   .values(**before))
                self._record_score(connection, request)
                actual = dict(connection.execute(select(m.operations).where(
                    m.operations.c.id == operation["id"])).mappings().one())
                self.assertEqual(actual, before)
        self.assertEqual(self.store.count("worker_operations"), 1)
        with self.store.engine.connect() as connection:
            self.assertFalse(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action == "COMPANY_DISCOVERY_REQUEST_PRIORITY_PROMOTED")).first())


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "disposable PostgreSQL not configured")
class CompanyDiscoveryPolicyPostgresTests(CompanyDiscoveryPolicyTests):
    """Exercise identical policy and promotion transactions in an isolated PG schema."""

    from tests.integration.test_lever_form_review_flow import (
        LeverFormReviewPostgresFlowTests as _Fixture,
    )
    _new_store = _Fixture._new_store
    _drop_schema = _Fixture._drop_schema

    def test_promotion_and_worker_claim_serialize_without_replacing_the_claim(self):
        self._assert_promotion_claim_order("promote")

    def test_worker_claim_wins_without_later_rescore_rewriting_running_params(self):
        self._assert_promotion_claim_order("claim")

    def _assert_promotion_claim_order(self, first):
        request, operation = self._queued_normal_request()
        owner_locked = threading.Event()
        contender_started = threading.Event()
        release_owner = threading.Event()
        local = threading.local()

        def is_operation_lock(statement):
            return "FROM worker_operations" in statement and "FOR UPDATE" in statement

        def before_execute(connection, cursor, statement, parameters, context, executemany):
            if getattr(local, "role", None) != first and is_operation_lock(statement):
                contender_started.set()

        def after_execute(connection, cursor, statement, parameters, context, executemany):
            if getattr(local, "role", None) == first and is_operation_lock(statement):
                owner_locked.set()
                if not release_owner.wait(5):
                    raise AssertionError("Synthetic operation-lock owner was not released")

        def run(role):
            local.role = role
            if role == "claim":
                return claim(self.store, operation["id"])
            with self.store.engine.begin() as connection:
                return self._record_score(connection, request)

        event.listen(self.store.engine, "before_cursor_execute", before_execute)
        event.listen(self.store.engine, "after_cursor_execute", after_execute)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                owner = pool.submit(run, first)
                try:
                    self.assertTrue(owner_locked.wait(5), "Owner did not lock the operation")
                    second = "claim" if first == "promote" else "promote"
                    contender = pool.submit(run, second)
                    self.assertTrue(contender_started.wait(5), "Contender did not attempt its lock")
                    self.assertFalse(contender.done())
                finally:
                    release_owner.set()
                owner_result = owner.result(timeout=5)
                contender_result = contender.result(timeout=5)
                claimed = owner_result if first == "claim" else contender_result
        finally:
            release_owner.set()
            event.remove(self.store.engine, "before_cursor_execute", before_execute)
            event.remove(self.store.engine, "after_cursor_execute", after_execute)
        self.assertEqual(claimed["id"], operation["id"])
        self.assertEqual(claimed["state"], "RUNNING")
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertEqual(claimed["params"]["dispatch_priority"],
                         "HIGH" if first == "promote" else "NORMAL")
        with self.store.engine.connect() as connection:
            persisted = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation["id"])).mappings().one())
            current = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
        self.assertEqual(persisted, claimed)
        self.assertEqual(current["operation_id"], operation["id"])
        self.assertIn("HIGH_MATCH_JOB", current["reasons"])


if __name__ == "__main__":
    unittest.main()
