"""A parked intent can use later bound evidence without replacing reviewed work."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.domain_acquisition import native_job_link
from services.company_discovery.requests import (
    approve_discovery_request, dismiss_discovery_request, record_discovery_request,
)
from services.discovery.scheduler import source_due_at
from tests.integration import test_official_domain_acquisition as contract
from tests.integration.test_greenhouse import RAW
from workers.tasks import execute_operation


class CandidateRefreshTests(unittest.TestCase):
    def setUp(self):
        self.case = contract.OfficialDomainAcquisitionTests()
        if hasattr(self, 'database_url'):
            self.case.database_url = self.database_url
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.sequence = 0

    def collect(self, inventory, profile_id="fixture"):
        case = self.case
        case.inventory = inventory
        with case.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.job_sources)).mappings().one())
            due = max(datetime.now(timezone.utc), source_due_at(source) + timedelta(seconds=1))
        class CollectionClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return due.astimezone(tz) if tz else due.replace(tzinfo=None)
        self.sequence += 1
        response = case.client.post("/sources/" + case.source_id + "/collect",
            json={"profile_id": profile_id, "request_id": "candidate-refresh-" + str(self.sequence)})
        self.assertEqual(response.status_code, 202, response.text)
        with patch("httpx.HTTPTransport.handle_request",
                   lambda transport, request: case._native_boundary(transport, request)), \
                patch("services.discovery.sources.collect.datetime", CollectionClock), \
                patch("services.discovery.sources.collect.now", return_value=due.isoformat()):
            outcome = execute_operation(response.json()["operation_id"])
        self.assertEqual(outcome["status"], "COMPLETED", outcome)
        with case.store.engine.connect() as connection:
            return dict(connection.execute(select(m.company_discovery_requests)).mappings().one())

    def first(self):
        return dict(RAW, absolute_url="https://job-boards.greenhouse.io/example/jobs/1234")

    def second(self, **changes):
        result = dict(RAW, id=1235, internal_job_id=5679,
                      absolute_url="https://employer.example/careers/python?gh_jid=1235")
        result.update(changes)
        return result

    def test_later_bound_job_origin_queues_one_high_priority_operation_and_survives_reopen(self):
        pending = self.collect([self.first()])
        self.assertEqual(pending["state"], "NEEDS_ORIGIN_REVIEW")
        self.assertIsNone(pending["operation_id"])
        self.assertIn("HIGH_MATCH_JOB", pending["reasons"])
        queued = self.collect([self.first(), self.second()])
        with self.case.store.engine.connect() as connection:
            second = connection.execute(select(m.jobs).where(m.jobs.c.requisition == "1235")).mappings().one()
            selected = second["attributes"]["source_resolution"]["selected_candidate_id"]
            incoming = dict(pending, candidate_id=selected, trigger_job_id=second["id"])
            self.assertIsNotNone(native_job_link(connection, incoming), "Later evidence itself is admissible")
        self.assertEqual(queued["state"], "QUEUED")
        self.assertEqual(queued["candidate_id"], selected)
        self.assertEqual(queued["trigger_job_id"], second["id"])
        self.assertEqual(queued["id"], pending["id"])
        self.assertEqual(queued["created_at"], pending["created_at"])
        self.assertEqual(queued["revision"], pending["revision"] + 1)
        self.assertEqual(queued["authorization"], "ATS_JOB_LINK_CANDIDATE")
        self.assertEqual(self.case.store.count("company_domains"), 0)
        self.assertEqual(self.case.employer_requests, [])
        self.case.store.engine.dispose()
        reopened = Store(self.case.url)
        self.addCleanup(reopened.engine.dispose)
        profile = reopened.profile("fixture")
        with reopened.engine.begin() as connection:
            record_discovery_request(connection, second["id"], profile)
            request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            self.assertEqual(request["candidate_id"], selected)
            self.assertEqual(request["operation_id"], queued["operation_id"])
            operations = list(connection.execute(select(m.operations).where(
                m.operations.c.kind == "COMPANY_DISCOVER")).mappings())
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]["params"]["dispatch_priority"], "HIGH")
            self.assertEqual(operations[0]["params"]["native_job_link"]["native_job_id"], "1235")
        with self.assertRaisesRegex(ValueError, "DISCOVERY_REQUEST_REVISION_CONFLICT"):
            approve_discovery_request(reopened, pending["id"], "https://review.example/", pending["revision"])
        self.assertEqual(len(self.case.requests), 4)

    def staged_candidate(self):
        pending = self.collect([self.first()])
        # Persist actual native observations while deferring intake once, so the
        # transaction/failure tests can exercise real admission independently.
        with patch("services.discovery.sources.collect.record_discovery_requests"):
            unchanged = self.collect([self.first(), self.second()])
        self.assertEqual(unchanged["candidate_id"], pending["candidate_id"])
        with self.case.store.engine.connect() as connection:
            job = dict(connection.execute(select(m.jobs).where(m.jobs.c.requisition == "1235")).mappings().one())
            selected = job["attributes"]["source_resolution"]["selected_candidate_id"]
            self.assertIsNotNone(native_job_link(connection, dict(
                pending, candidate_id=selected, trigger_job_id=job["id"])))
        return pending, job

    def assert_still_parked(self, pending):
        with self.case.store.engine.connect() as connection:
            actual = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            for key in ("candidate_id", "trigger_job_id", "revision", "profile_id", "profile_version",
                        "created_at", "selected_website", "authorization", "domain_evidence", "operation_id"):
                self.assertEqual(actual[key], pending[key], key)
            self.assertEqual(connection.execute(select(m.operations).where(
                m.operations.c.kind == "COMPANY_DISCOVER")).mappings().all(), [])
            self.assertEqual(connection.execute(select(m.audit).where(
                m.audit.c.action == "COMPANY_DISCOVERY_REQUEST_CANDIDATE_REFRESHED")).mappings().all(), [])

    def test_invalid_later_link_keeps_the_parked_binding(self):
        pending = self.collect([self.first()])
        self.collect([self.first(), self.second(absolute_url="https://job-boards.greenhouse.io/example/jobs/1235")])
        self.assert_still_parked(pending)

    def test_absent_or_different_incoming_profile_cannot_replace_the_binding(self):
        pending, job = self.staged_candidate()
        profile = self.case.store.profile("fixture")
        for incoming in (None, dict(profile, id="different-profile"), dict(profile, version="different-version")):
            with self.subTest(profile=None if incoming is None else (incoming["id"], incoming["version"])):
                with self.case.store.engine.begin() as connection:
                    record_discovery_request(connection, job["id"], incoming,
                                             explicit_reason="USER_PREFERRED_COMPANY")
                self.assert_still_parked(pending)

    def test_changed_saved_profile_rejects_a_stale_matching_caller_snapshot(self):
        pending, job = self.staged_candidate()
        stale = self.case.store.profile("fixture")
        changed = deepcopy(contract.PROFILE)
        changed["name"] = "Changed fixture profile"
        changed["expected_version"] = stale["version"]
        response = self.case.client.put("/profiles", json=changed)
        self.assertEqual(response.status_code, 200, response.text)
        with self.case.store.engine.begin() as connection:
            record_discovery_request(connection, job["id"], stale, explicit_reason="USER_PREFERRED_COMPANY")
        self.assert_still_parked(pending)

    def test_disabled_source_or_tampered_observation_cannot_supply_replacement_evidence(self):
        pending, job = self.staged_candidate()
        profile = self.case.store.profile("fixture")
        with self.case.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).values(enabled=False))
            record_discovery_request(connection, job["id"], profile)
        self.assert_still_parked(pending)
        with self.case.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).values(enabled=True))
            posting = connection.execute(select(m.postings).where(m.postings.c.job_id == job["id"])).mappings().one()
            connection.execute(update(m.postings).where(m.postings.c.id == posting["id"]).values(
                raw={**posting["raw"], "absolute_url": "https://changed.example/jobs/1235"}))
            record_discovery_request(connection, job["id"], profile)
        self.assert_still_parked(pending)

    def test_selected_candidate_drift_at_the_locked_boundary_prevents_refresh(self):
        pending, job = self.staged_candidate()
        profile = self.case.store.profile("fixture")
        def change_winner(connection, request):
            attributes = deepcopy(job["attributes"])
            attributes["source_resolution"]["selected_candidate_id"] = None
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(attributes=attributes))
            return native_job_link(connection, request)
        # Deterministic interleaving at the proof boundary; not a timing-based
        # assertion about concurrent transaction execution.
        with self.case.store.engine.begin() as connection, patch(
                "services.company_discovery.domain_acquisition.native_job_link", side_effect=change_winner):
            record_discovery_request(connection, job["id"], profile)
        self.assert_still_parked(pending)

    def test_failed_queue_rolls_back_candidate_and_refresh_audit(self):
        pending, job = self.staged_candidate()
        profile = self.case.store.profile("fixture")
        with self.assertRaisesRegex(RuntimeError, "synthetic queue failure"):
            with self.case.store.engine.begin() as connection, patch(
                    "services.company_discovery.requests.enqueue_operation",
                    side_effect=RuntimeError("synthetic queue failure")):
                record_discovery_request(connection, job["id"], profile)
        self.assert_still_parked(pending)
        with self.case.store.engine.begin() as connection:
            record_discovery_request(connection, job["id"], profile)
            current = connection.execute(select(m.company_discovery_requests)).mappings().one()
            self.assertEqual(current["state"], "QUEUED")
            audit = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action == "COMPANY_DISCOVERY_REQUEST_CANDIDATE_REFRESHED")).scalar_one()
            self.assertEqual(audit["revision"], pending["revision"])
            self.assertEqual(audit["new_revision"], current["revision"])
            self.assertEqual(audit["previous_candidate_id"], pending["candidate_id"])
            self.assertEqual(audit["candidate_id"], current["candidate_id"])

    def test_dismissed_request_keeps_its_binding_and_revision(self):
        pending = self.collect([self.first()])
        dismiss_discovery_request(self.case.store, pending["id"], pending["revision"])
        after = self.collect([self.first(), self.second()])
        self.assertEqual(after["state"], "DISMISSED")
        self.assertEqual(after["candidate_id"], pending["candidate_id"])
        self.assertEqual(after["revision"], pending["revision"] + 1)
        self.assertIsNone(after["operation_id"])

    def test_authorized_active_and_terminal_operations_keep_their_original_evidence(self):
        original = self.collect([dict(RAW, absolute_url=contract.JOB_URL)])
        self.collect([dict(RAW, absolute_url=contract.JOB_URL), self.second()])
        profile = self.case.store.profile("fixture")
        with self.case.store.engine.connect() as connection:
            job = connection.execute(select(m.jobs).where(m.jobs.c.requisition == "1235")).mappings().one()
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == original["operation_id"])).mappings().one())
        for state in ("QUEUED", "RUNNING", "COMPLETED", "BLOCKED", "CANCELLED", "DEAD_LETTERED"):
            with self.subTest(state=state), self.case.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation["id"]).values(state=state))
                record_discovery_request(connection, job["id"], profile)
                current = connection.execute(select(m.company_discovery_requests)).mappings().one()
                for key in ("candidate_id", "trigger_job_id", "revision", "selected_website",
                            "authorization", "domain_evidence", "operation_id", "profile_id", "profile_version"):
                    self.assertEqual(current[key], original[key], key)
                operations = list(connection.execute(select(m.operations).where(
                    m.operations.c.kind == "COMPANY_DISCOVER")).mappings())
                self.assertEqual(len(operations), 1)
                self.assertEqual(operations[0]["params"], operation["params"])
                self.assertEqual(operations[0]["attempt_count"], operation["attempt_count"])
