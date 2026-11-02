"""Fresh bounded aggregator checks precede synthetic admission, never a real send."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import delete, func, select, update

from fixtures.mock_server import fixture_server
from packages.domain.safety import Policy
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.applications import submission_admission as admission
from services.dedupe.routes import qualify_route_authority
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.operations import OperationCancelled, OperationTimedOut


class SubmissionRoutePreflightTests(unittest.TestCase):
    def _new_store(self):
        store = Store(f"sqlite:///{(self.root / 'route-check.db').as_posix()}")
        store.migrate()
        return store

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mjobs-route-preflight-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = self._new_store()
        self.addCleanup(self.store.engine.dispose)
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.application_id = prepare(
            self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.store.transition(self.application_id, ["PREPARED"], "READY_TO_SUBMIT")
        self.application = self.store.application(self.application_id)
        self.route_id = self.application["route_id"]
        self.job_id = self.application["job_id"]
        self.source_url = "https://board.example/jobs/42?private=PRIVATE_SOURCE_TOKEN"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id).values(target="REAL"))
            self.company_id = connection.execute(select(m.jobs.c.company_id).where(
                m.jobs.c.id == self.job_id)).scalar_one()
            connection.execute(update(m.routes).where(m.routes.c.id == self.route_id).values(
                provider="job-board", tenant="", url=self.source_url))
            upsert(connection, m.company_ats_tenants, {
                "id": "submission-route-tenant", "company_id": self.company_id,
                "provider": "greenhouse", "tenant_identifier": "verified-employer",
                "organization_identifier": "", "board_identifier": "",
                "confidence": .95, "evidence": ["PUBLIC_API_BOARD"],
                "first_seen_at": "2026-11-02T00:00:00Z",
                "last_seen_at": "2026-11-02T00:00:00Z",
            })
            self.store._audit(connection, self.application_id, "APPLICATION_QUESTION_PREFLIGHT", {
                "result": "PASS", "reason": None,
                "profile_version": self.application["profile_version"],
                "document_version": self.application["document_version"],
                "route_id": self.route_id, "missing": [], "answer_review": [],
            })
        self.now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.receipt_id = "synthetic-route-submission-intent"
        self.policy = Policy(external_submissions_enabled=True)
        admission.grant_live_submission_intent(
            self.store, receipt_id=self.receipt_id, profile_id="fixture",
            application_id=self.application_id, job_id=self.job_id,
            granted_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=5), source_allowed=True)

    def authorize(self, *, policy=None, check=None, receipt_id=None, barrier=None):
        with patch.object(admission, "_utc_now", side_effect=lambda: self.now):
            return admission.authorize_submission(
                self.store, self.application_id, 100, self.policy if policy is None else policy,
                intent_receipt_id=receipt_id or self.receipt_id, barrier=barrier, check=check)

    def consumed(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action == admission.CONSUMED)).scalar_one()

    def checks(self, store=None):
        with (store or self.store).engine.connect() as connection:
            return list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == admission.ROUTE_CHECKED)).scalars())

    def resolved(self):
        return {"status": "RESOLVED_ATS_ROUTE", "provider": "greenhouse",
                "tenant": "verified-employer",
                "candidate_url": "https://boards.greenhouse.io/verified-employer/jobs/42"}

    def assert_unclaimed(self):
        self.assertEqual(self.consumed(), 0)
        self.assertEqual(self.store.application(self.application_id)["state"], "READY_TO_SUBMIT")

    def register_integrated_board(self, provider, *, route_url=None):
        """Persist the exact source/capability/posting chain used by qualification."""
        source_id = "route-preflight-source-" + provider
        endpoint = "https://" + provider + ".example/jobs.json"
        route_url = route_url or self.source_url
        with self.store.engine.begin() as connection:
            upsert(connection, m.job_sources, {
                "id": source_id, "provider": provider, "tenant": "public",
                "name": "Synthetic integrated board", "enabled": True,
                "strategy": "PUBLIC_JSON", "capabilities": {
                    "strategy": "PUBLIC_JSON",
                    "capabilities": ["DISCOVERY", "APPLICATION_REDIRECT"],
                },
                "poll_interval": 300, "concurrency": 1, "request_timeout": 10,
                "rate_limit": 60, "jitter_seconds": 0, "health_status": "NOT_CHECKED",
            })
            upsert(connection, m.company_sources, {
                "id": "route-preflight-company-source-" + provider,
                "company_id": self.company_id, "provider": provider,
                "tenant": "public", "endpoint": endpoint,
                "discovered_automatically": True, "collector_source_id": source_id,
                "public_api_url": endpoint, "collection_strategy": "PUBLIC_JSON",
            })
            upsert(connection, m.postings, {
                "id": "route-preflight-posting-" + provider, "job_id": self.job_id,
                "source": endpoint, "source_job_id": "synthetic-board-job",
                "source_url": endpoint, "application_url": route_url,
                "raw": {}, "fetched_at": "2026-11-02T00:00:00Z",
            })
            connection.execute(update(m.routes).where(m.routes.c.id == self.route_id).values(
                provider=provider, tenant="public", url=route_url))
        self.assertEqual(self.route_authority(), "TRUSTED_INTEGRATED_BOARD")
        return source_id

    def route_authority(self):
        with self.store.engine.connect() as connection:
            route = connection.execute(select(m.routes).where(
                m.routes.c.id == self.route_id)).mappings().one()
            return qualify_route_authority(connection, [dict(route)])[0]["authority"]

    def test_fresh_probe_precedes_claim_without_holding_database_write_locks(self):
        calls = []

        def probe(url, *, verified_domains, ats_tenants, check):
            calls.append(url)
            self.assert_unclaimed()
            self.assertEqual(self.checks(), [])
            self.assertTrue(any(row["tenant_identifier"] == "verified-employer" for row in ats_tenants))
            # A separate writer must finish while the resolver is active. This
            # would block/deadlock if admission retained its first transaction.
            with ThreadPoolExecutor(max_workers=1) as pool:
                def independent_writer():
                    with self.store.engine.begin() as connection:
                        connection.execute(update(m.routes).where(
                            m.routes.c.id == self.route_id).values(url=self.source_url))
                pool.submit(independent_writer).result(timeout=3)
            return {**self.resolved(), "body": "PRIVATE_BODY", "token": "PRIVATE_SECRET"}

        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            decision = self.authorize()
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertEqual(calls, [self.source_url])
        self.assertEqual(self.consumed(), 1)
        self.assertEqual(self.store.application(self.application_id)["route_id"], self.route_id)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.routes.c.url).where(
                m.routes.c.id == self.route_id)).scalar_one(), self.source_url)
        reopened = Store(self.store.engine.url.render_as_string(hide_password=False))
        try:
            records = self.checks(reopened)
        finally:
            reopened.engine.dispose()
        self.assertEqual(records[0]["status"], "RESOLVED_ATS_ROUTE")
        self.assertEqual(len(records[0]["candidate_url_sha256"]), 64)
        self.assertNotRegex(json.dumps(records), "PRIVATE|https:|candidate_url\"")

    def test_default_off_missing_intent_barriers_and_caps_never_probe(self):
        with patch.object(admission, "resolve_aggregator_redirect") as probe:
            for options in (
                {"policy": Policy()}, {"receipt_id": "missing"},
                {"barrier": "CAPTCHA"}, {"barrier": "MFA"},
                {"policy": Policy(external_submissions_enabled=True, hourly_limit=0)},
            ):
                with self.subTest(options=options):
                    self.assertFalse(self.authorize(**options).allowed)
            with patch.object(admission, "_utc_now", side_effect=lambda: self.now):
                configured = admission.authorize_submission(
                    self.store, self.application_id, 100,
                    intent_receipt_id=self.receipt_id)
                self.assertIn("EXTERNAL_SUBMISSION_DISABLED", configured.reasons)
            probe.assert_not_called()
        self.assert_unclaimed()

    def test_mock_and_verified_ats_admission_do_not_probe(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == self.route_id).values(
                provider="greenhouse", tenant="verified-employer", url=self.resolved()["candidate_url"]))
        with patch.object(admission, "resolve_aggregator_redirect") as probe:
            self.assertTrue(self.authorize().allowed)
            probe.assert_not_called()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id).values(
                    target="MOCK", state="READY_TO_SUBMIT", submitted_at=None))
        with patch.object(admission, "resolve_aggregator_redirect") as probe:
            self.assertTrue(self.authorize().allowed)
            probe.assert_not_called()

    def test_trusted_integrated_boards_still_require_a_fresh_probe(self):
        for provider in ("remotive", "remoteok", "remote_ok", "wwr"):
            with self.subTest(provider=provider):
                self.register_integrated_board(provider)
                with patch.object(admission, "resolve_aggregator_redirect", return_value={
                        "status": "BLOCKED", "reason": "ROBOTS_DENIED"}) as probe:
                    decision = self.authorize()
                probe.assert_called_once()
                self.assertEqual(probe.call_args.args, (self.source_url,))
                self.assertIn("APPLICATION_ROUTE_DISCOVERY_BLOCKED", decision.reasons)
                self.assert_unclaimed()
        self.assertEqual(len(self.checks()), 4)
        self.assertTrue(all(row["status"] == "BLOCKED" for row in self.checks()))

    def test_trusted_board_completed_unresolved_probe_preserves_selected_route(self):
        self.register_integrated_board("remotive")
        with patch.object(admission, "resolve_aggregator_redirect", return_value={
                "status": "UNRESOLVED"}) as probe:
            decision = self.authorize()
        self.assertTrue(decision.allowed, decision.reasons)
        probe.assert_called_once()
        self.assertEqual(self.consumed(), 1)
        self.assertEqual(self.checks()[0]["status"], "UNRESOLVED")
        self.assertEqual(self.store.application(self.application_id)["route_id"], self.route_id)

    def test_unverified_provider_labels_cannot_exempt_the_board_url(self):
        for provider, tenant, authority in (
            ("greenhouse", "verified-employer", "UNKNOWN"),
            ("ashby", "verified-employer", "VERIFIED_ATS"),
            ("workday", "verified-employer", "VERIFIED_ATS"),
            ("employer", "", "UNKNOWN"),
        ):
            with self.subTest(provider=provider):
                with self.store.engine.begin() as connection:
                    if provider in {"ashby", "workday"}:
                        upsert(connection, m.company_ats_tenants, {
                            "id": "route-label-tenant-" + provider,
                            "company_id": self.company_id, "provider": provider,
                            "tenant_identifier": tenant, "organization_identifier": "",
                            "board_identifier": "", "confidence": .95,
                            "evidence": ["PUBLIC_API_BOARD"],
                            "first_seen_at": "2026-11-02T00:00:00Z",
                            "last_seen_at": "2026-11-02T00:00:00Z",
                        })
                    connection.execute(update(m.routes).where(m.routes.c.id == self.route_id).values(
                        provider=provider, tenant=tenant))
                self.assertEqual(self.route_authority(), authority)
                with patch.object(admission, "resolve_aggregator_redirect", return_value={
                        "status": "BLOCKED", "reason": "ROBOTS_DENIED"}) as probe:
                    decision = self.authorize()
                probe.assert_called_once()
                self.assertIn("APPLICATION_ROUTE_DISCOVERY_BLOCKED", decision.reasons)
                self.assert_unclaimed()

    def test_board_source_with_verified_ats_destination_does_not_probe(self):
        self.register_integrated_board("remotive", route_url=self.resolved()["candidate_url"])
        with patch.object(admission, "resolve_aggregator_redirect") as probe:
            decision = self.authorize()
        self.assertTrue(decision.allowed, decision.reasons)
        probe.assert_not_called()
        self.assertEqual(self.checks(), [])

    def test_board_source_with_verified_employer_destination_does_not_probe(self):
        self.register_integrated_board(
            "remoteok", route_url="https://careers.verified.example/jobs/42")
        with self.store.engine.begin() as connection:
            upsert(connection, m.company_domains, {
                "id": "route-preflight-verified-domain", "company_id": self.company_id,
                "domain": "verified.example", "confidence": .95, "verified": True,
                "verification_method": "SYNTHETIC_TEST_EVIDENCE",
                "first_seen_at": "2026-11-02T00:00:00Z",
                "last_verified_at": "2026-11-02T00:00:00Z", "evidence": ["VERIFIED_DOMAIN"],
            })
        self.assertEqual(self.route_authority(), "VERIFIED_EMPLOYER")
        with patch.object(admission, "resolve_aggregator_redirect") as probe:
            decision = self.authorize()
        self.assertTrue(decision.allowed, decision.reasons)
        probe.assert_not_called()
        self.assertEqual(self.checks(), [])

    def test_revoked_integrated_board_capability_invalidates_the_probe(self):
        source_id = self.register_integrated_board("remotive")

        def probe(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.job_sources).where(
                    m.job_sources.c.id == source_id).values(capabilities={
                        "strategy": "PUBLIC_JSON", "capabilities": ["DISCOVERY"],
                    }))
            self.assertEqual(self.route_authority(), "AGGREGATOR")
            return {"status": "UNRESOLVED"}

        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("APPLICATION_ROUTE_CHANGED", self.authorize().reasons)
        self.assert_unclaimed()
        self.assertEqual(self.checks()[0]["status"], "STALE")

    def test_unresolved_attempt_is_recorded_and_keeps_the_selected_route(self):
        with patch.object(admission, "resolve_aggregator_redirect",
                          return_value={"status": "UNRESOLVED"}) as probe:
            self.assertTrue(self.authorize().allowed)
            probe.assert_called_once()
        self.assertEqual(self.checks()[0]["status"], "UNRESOLVED")
        self.assertEqual(self.store.application(self.application_id)["route_id"], self.route_id)

    def test_blocked_and_invalid_probe_results_fail_closed_without_private_output(self):
        for result in (
            {"status": "BLOCKED", "reason": "FETCH_DEADLINE"},
            {"status": "BLOCKED", "reason": "PRIVATE_EXCEPTION_TOKEN"},
            {**self.resolved(), "candidate_url": "https://unverified.example/private"},
            {**self.resolved(), "candidate_url": self.resolved()["candidate_url"] + "?secret=PRIVATE"},
            {"status": "PRIVATE_STATUS"}, {"status": ["invalid"]}, None,
        ):
            with self.subTest(result=result), patch.object(
                    admission, "resolve_aggregator_redirect", return_value=result):
                decision = self.authorize()
                self.assertIn("APPLICATION_ROUTE_DISCOVERY_BLOCKED", decision.reasons)
                self.assert_unclaimed()
        self.assertNotRegex(json.dumps(self.checks()), "PRIVATE|https:")

    def test_changed_route_during_probe_invalidates_result(self):
        def probe(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.routes).where(m.routes.c.id == self.route_id).values(
                    url="https://board.example/changed"))
            return self.resolved()
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("APPLICATION_ROUTE_CHANGED", self.authorize().reasons)
        self.assert_unclaimed()
        self.assertEqual(self.checks()[0]["status"], "STALE")

    def test_revoked_tenant_evidence_during_probe_invalidates_result(self):
        def probe(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_ats_tenants).where(
                    m.company_ats_tenants.c.id == "submission-route-tenant").values(confidence=.1))
            return self.resolved()
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("APPLICATION_ROUTE_CHANGED", self.authorize().reasons)
        self.assert_unclaimed()

    def test_changed_profile_version_during_probe_invalidates_result(self):
        def probe(*args, **kwargs):
            self.store.save_profile(dict(PROFILE, name="Changed synthetic profile"))
            return {"status": "UNRESOLVED"}
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("APPLICATION_ROUTE_CHANGED", self.authorize().reasons)
        self.assert_unclaimed()

    def test_current_materials_are_revalidated_after_probe(self):
        def probe(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(delete(m.audit).where(
                    m.audit.c.entity_id == self.application_id,
                    m.audit.c.action == "DOCUMENTS_VALIDATED"))
            return self.resolved()
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("QUALITY_REVIEW_REQUIRED", self.authorize().reasons)
        self.assert_unclaimed()

    def test_updated_and_revalidated_document_hashes_cannot_reuse_the_probe(self):
        def probe(*args, **kwargs):
            with self.store.engine.begin() as connection:
                documents = list(connection.execute(select(m.documents).where(
                    m.documents.c.application_id == self.application_id)).mappings())
                changed_hashes = {"pdf": "a" * 64, "docx": "b" * 64}
                changed_id = documents[0]["id"]
                connection.execute(update(m.documents).where(
                    m.documents.c.id == changed_id).values(sha256=changed_hashes))
                event = connection.execute(select(m.audit).where(
                    m.audit.c.entity_id == self.application_id,
                    m.audit.c.action == "DOCUMENTS_VALIDATED")).mappings().one()
                payload = dict(event["payload"])
                payload["documents"] = [
                    {**row, "sha256": changed_hashes} if row["id"] == changed_id else row
                    for row in payload["documents"]]
                connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(
                    payload=payload))
            return {"status": "UNRESOLVED"}
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("APPLICATION_ROUTE_CHANGED", self.authorize().reasons)
        self.assert_unclaimed()

    def test_submission_caps_are_recounted_after_probe(self):
        def probe(*args, **kwargs):
            job = next(row for row in self.store.jobs("fixture") if row["id"] != self.job_id)
            other = prepare(self.store, job["id"], "fixture", self.root)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.applications).where(m.applications.c.id == other).values(
                    state="SUBMITTING", submitted_at=self.now.isoformat()))
            return {"status": "UNRESOLVED"}
        policy = Policy(external_submissions_enabled=True, hourly_limit=1)
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("HOURLY_LIMIT_REACHED", self.authorize(policy=policy).reasons)
        self.assert_unclaimed()

    def test_intent_expiring_during_probe_cannot_be_consumed(self):
        def probe(*args, **kwargs):
            self.now += timedelta(minutes=6)
            return self.resolved()
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            self.assertIn("STALE_JOB_INTENT", self.authorize().reasons)
        self.assert_unclaimed()

    def _expire_intent_after_route_locks(self, *, direct_destination):
        if direct_destination:
            with self.store.engine.begin() as connection:
                connection.execute(update(m.routes).where(
                    m.routes.c.id == self.route_id).values(
                        url=self.resolved()["candidate_url"]))
        original_snapshot = admission._route_check_snapshot
        calls = 0

        def delayed_snapshot(*args, **kwargs):
            nonlocal calls
            result = original_snapshot(*args, **kwargs)
            calls += 1
            if calls == (1 if direct_destination else 2):
                # Model time spent acquiring route/evidence locks after the
                # transaction's initial consent check, without sleeping.
                self.now += timedelta(minutes=6)
            return result

        with patch.object(admission, "_route_check_snapshot", side_effect=delayed_snapshot), \
                patch.object(admission, "resolve_aggregator_redirect",
                             return_value=self.resolved()) as probe:
            decision = self.authorize()
        self.assertFalse(decision.allowed, decision.reasons)
        self.assertIn("STALE_JOB_INTENT", decision.reasons)
        self.assertEqual(probe.call_count, 0 if direct_destination else 1)
        self.assert_unclaimed()

    def test_intent_expiring_after_probe_while_route_locks_wait_cannot_be_consumed(self):
        self._expire_intent_after_route_locks(direct_destination=False)

    def test_direct_destination_cannot_consume_intent_expired_during_route_locks(self):
        self._expire_intent_after_route_locks(direct_destination=True)

    def test_wrong_ats_tenant_in_path_cannot_exempt_or_validate_destination(self):
        for provider, candidate in (
            ("ashby", "https://jobs.ashbyhq.com/other/verified-employer"),
            ("workday", "https://other.myworkdayjobs.com/en-US/verified-employer/job/42"),
        ):
            with self.subTest(provider=provider):
                with self.store.engine.begin() as connection:
                    upsert(connection, m.company_ats_tenants, {
                        "id": "wrong-tenant-" + provider, "company_id": self.company_id,
                        "provider": provider, "tenant_identifier": "verified-employer",
                        "organization_identifier": "other", "board_identifier": "42",
                        "confidence": .95, "evidence": ["PUBLIC_API_BOARD"],
                        "first_seen_at": "2026-11-02T00:00:00Z",
                        "last_seen_at": "2026-11-02T00:00:00Z",
                    })
                    connection.execute(update(m.routes).where(
                        m.routes.c.id == self.route_id).values(url=candidate))
                with patch.object(admission, "resolve_aggregator_redirect", return_value={
                    "status": "RESOLVED_ATS_ROUTE", "provider": provider,
                    "tenant": "verified-employer", "candidate_url": candidate,
                }) as probe:
                    decision = self.authorize()
                probe.assert_called_once()
                self.assertFalse(decision.allowed, decision.reasons)
                self.assertIn("APPLICATION_ROUTE_DISCOVERY_BLOCKED", decision.reasons)
                self.assert_unclaimed()
                self.assertEqual(self.checks()[-1]["reason"], "DESTINATION_NOT_VERIFIED")

    def test_cancellation_and_operation_deadline_propagate_without_claim(self):
        for error in (OperationCancelled("cancelled"), OperationTimedOut("expired")):
            with self.subTest(error=type(error).__name__):
                cancelled = False
                def check():
                    if cancelled:
                        raise error
                def probe(*args, **kwargs):
                    nonlocal cancelled
                    self.assertIs(kwargs["check"], check)
                    cancelled = True
                    kwargs["check"]()
                with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
                    with self.assertRaises(type(error)):
                        self.authorize(check=check)
                self.assert_unclaimed()
        self.assertEqual(self.checks(), [])

    def test_post_probe_cancellation_cannot_claim_a_successful_result(self):
        cancelled = False
        def check():
            if cancelled:
                raise OperationCancelled("cancelled after response")
        def probe(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return self.resolved()
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe):
            with self.assertRaises(OperationCancelled):
                self.authorize(check=check)
        self.assert_unclaimed()
        self.assertEqual(self.checks(), [])

    def test_expired_probe_is_not_admitted_after_database_wait(self):
        times = iter([100.0, 109.0])
        with patch.object(admission, "resolve_aggregator_redirect", return_value=self.resolved()), \
                patch.object(admission, "time", SimpleNamespace(monotonic=lambda: next(times))):
            self.assertIn("APPLICATION_ROUTE_CHECK_EXPIRED", self.authorize().reasons)
        self.assert_unclaimed()
        self.assertEqual(self.checks()[0]["status"], "STALE")

    def test_concurrent_probes_have_exactly_one_claim_and_consumption(self):
        barrier = threading.Barrier(2)
        def probe(*args, **kwargs):
            barrier.wait(timeout=5)
            return self.resolved()
        with patch.object(admission, "resolve_aggregator_redirect", side_effect=probe), \
                patch.object(admission, "_utc_now", side_effect=lambda: self.now), \
                ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(lambda _: admission.authorize_submission(
                self.store, self.application_id, 100, self.policy,
                intent_receipt_id=self.receipt_id), range(2)))
        self.assertEqual(sum(result.allowed for result in decisions), 1)
        self.assertEqual(self.consumed(), 1)
        self.assertEqual(len(self.checks()), 1)


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Disposable PostgreSQL URL is required")
class SubmissionRoutePreflightPostgresTests(SubmissionRoutePreflightTests):
    from tests.integration.test_lever_form_review_flow import LeverFormReviewPostgresFlowTests as _Fixture
    _new_store = _Fixture._new_store
    _drop_schema = _Fixture._drop_schema
