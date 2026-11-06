"""Collection stages bounded untrusted hints; employer network belongs to workers."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.errors import SourceError
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE
from workers.operations import OperationCancelled, claim
from workers.dispatch import enqueue_operation


class AggregatorBootstrapCollectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-bootstrap-collection-")
        self.addCleanup(directory.cleanup)
        self.url = "sqlite:///" + (Path(directory.name) / "test.db").as_posix()
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.source_id = configure_source(self.store, "default", provider="remotive")
        self.rows = [deepcopy(RAW_REMOTIVE_PAYLOAD["jobs"][0])]
        self.http_requests = []
        self.after_feed_read = None

    def collect(self, profile_id="fixture", check=None, **lineage):
        def feed(request):
            self.http_requests.append(str(request.url))
            self.assertEqual((request.method, request.url.host, request.url.path),
                             ("GET", "remotive.com", "/api/remote-jobs"))
            if self.after_feed_read:
                self.after_feed_read()
            return httpx.Response(200, json={"jobs": self.rows, "job-count": len(self.rows)})
        with patch.object(PublicCompanyClient, "_request_once",
                          side_effect=AssertionError("No employer network in collection")):
            return collect_source(self.store, self.source_id, profile_id,
                                  transport=httpx.MockTransport(feed), check=check, **lineage)

    def parent_operation(self, *, scheduled=False):
        params = {"source_id": self.source_id, "request_id": "lineage-" + uuid4().hex}
        if scheduled:
            params["profile_ids"] = ["fixture"]
        else:
            params["profile_version"] = self.store.profile("fixture")["version"]
        identifier = enqueue_operation(self.store, "DISCOVER", "fixture", params)
        operation = claim(self.store, identifier)
        self.assertIsNotNone(operation)
        return {"parent_operation_id": identifier,
                "parent_operation_attempt": operation["attempt_count"]}

    def pending(self, store=None):
        with (store or self.store).engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.company_discovery_requests)).mappings()
                    if row["authorization"] == "AGGREGATOR_BOOTSTRAP_PENDING"]

    def test_collection_stages_exact_candidate_without_granting_employer_authority(self):
        original = deepcopy(self.rows[0])
        result = self.collect()
        self.assertEqual(result["jobs_seen"], 1)
        self.assertEqual(len(self.http_requests), 1)
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        request = pending[0]
        self.assertEqual(request["profile_id"], "fixture")
        self.assertEqual(request["profile_version"], self.store.profile("fixture")["version"])
        self.assertEqual(request["state"], "QUEUED")
        self.assertNotIn("parent_operation_id", json.dumps(request["domain_evidence"]))
        self.assertNotIn("parent_operation_attempt", json.dumps(request["domain_evidence"]))
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(self.pending(reopened), pending)
        with reopened.engine.connect() as connection:
            snapshot = connection.execute(select(m.posting_snapshots)).mappings().one()
            candidate = connection.execute(select(m.posting_value_candidates)).mappings().one()
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == request["operation_id"])).mappings().one()
            sources = list(connection.execute(select(m.job_sources)).mappings())
        self.assertEqual(snapshot["raw"], original)
        self.assertEqual(candidate["id"], request["candidate_id"])
        self.assertEqual(candidate["posting_snapshot_id"], snapshot["id"])
        self.assertEqual(operation["kind"], "COMPANY_DISCOVER")
        self.assertEqual(operation["params"]["aggregator_bootstrap"], "AGGREGATOR_BOOTSTRAP_PENDING_V1")
        self.assertEqual([row["provider"] for row in sources], ["remotive"])
        self.assertEqual(reopened.count("company_domains"), 0)
        self.assertEqual(reopened.count("applications"), 0)

    def test_cap_is_two_distinct_employers_without_truncating_collected_jobs(self):
        template = self.rows[0]
        self.rows = []
        for index, company in enumerate(("Alpha", "Alpha", "Bravo", "Charlie", "Delta")):
            identifier = 2000 + index
            self.rows.append(dict(template, id=identifier, company_name=company,
                title=f"Python Engineer {index}",
                url=f"https://remotive.com/remote-jobs/software-dev/python-engineer-{identifier}"))
        result = self.collect()
        self.assertEqual(result["jobs_seen"], 5)
        self.assertEqual(self.store.count("posting_snapshots"), 5)
        pending = self.pending()
        self.assertEqual(len(pending), 2)
        self.assertEqual(len({row["company_id"] for row in pending}), 2)
        self.assertEqual(len(self.http_requests), 1)

    def test_ambiguous_hints_consume_the_bounded_intake_without_scanning_more_employers(self):
        from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint
        template = self.rows[0]
        self.rows = [dict(template, id=3000 + index, company_name=f"Company {index}",
            url=f"https://remotive.com/remote-jobs/software-dev/engineer-{3000 + index}",
            description='<a href="https://job-boards.greenhouse.io/one/jobs/1">Apply</a>'
                        '<a href="https://job-boards.greenhouse.io/two/jobs/2">Apply</a>')
            for index in range(5)]
        with patch("services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint",
                   wraps=extract_bootstrap_hint) as extract:
            result = self.collect()
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(result["jobs_seen"], 5)
        self.assertEqual(self.store.count("posting_snapshots"), 5)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.http_requests), 1)

    def test_inbound_proof_fields_remain_raw_data_and_do_not_supply_authority(self):
        self.rows[0]["aggregator_bootstrap_candidate"] = {
            "schema": "AGGREGATOR_BOOTSTRAP_HINT_V1", "candidate_url": "https://attacker.example/apply"}
        self.rows[0]["domain_evidence"] = {
            "schema": "AGGREGATOR_NATIVE_BOOTSTRAP_V1", "verified": True,
            "employer_url": "https://attacker.example/apply"}
        original = deepcopy(self.rows[0])
        self.collect()
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        self.assertNotIn("attacker.example", json.dumps(pending[0]["domain_evidence"]))
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.posting_snapshots.c.raw)).scalar_one(), original)
        self.assertEqual(self.store.count("company_domains"), 0)

    def test_disabled_profile_policy_collects_without_bootstrap_queue(self):
        profile = deepcopy(PROFILE)
        profile["preferences"] = {"company_discovery": {"company_discovery_enabled": False}}
        self.store.save_profile(profile)
        self.assertEqual(self.collect()["jobs_seen"], 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_profile_revision_during_feed_read_does_not_stage_stale_intent(self):
        profile = deepcopy(PROFILE)
        profile["name"] = "Updated Fixture"
        self.after_feed_read = lambda: self.store.save_profile(profile)
        self.collect()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_collection_without_profile_does_not_stage_bootstrap(self):
        self.assertEqual(self.collect(profile_id=None)["jobs_seen"], 1)
        self.assertEqual(self.pending(), [])

    def test_known_employer_below_current_profile_match_threshold_is_not_parsed(self):
        from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint
        self.collect(profile_id=None)
        profile = deepcopy(PROFILE)
        profile["facts"] = []
        self.store.save_profile(profile)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source_id).values(last_attempt=None))
        with patch("services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint",
                   wraps=extract_bootstrap_hint) as extract:
            self.collect()
        extract.assert_not_called()
        self.assertEqual(self.pending(), [])

    def test_source_revocation_during_feed_read_rolls_back_intake_and_staged_intent(self):
        self.after_feed_read = lambda: configure_source(self.store, "default", provider="remotive", enabled=False)
        with self.assertRaisesRegex(SourceError, "SOURCE_DISABLED"):
            self.collect()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.count("posting_snapshots"), 0)
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_cancellation_during_hint_extraction_rolls_back_without_queueing(self):
        from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint
        cancelled = False
        def extract(raw, normalized):
            nonlocal cancelled
            result = extract_bootstrap_hint(raw, normalized)
            cancelled = True
            return result
        def check():
            if cancelled:
                raise OperationCancelled("synthetic collection cancellation")
        with patch("services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint", side_effect=extract):
            with self.assertRaises(OperationCancelled):
                self.collect(check=check)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.count("posting_snapshots"), 0)
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_pure_hint_parsing_does_not_hold_the_database_write_lock(self):
        def probe(_raw, _normalized):
            connection = sqlite3.connect(self.url.removeprefix("sqlite:///"), timeout=0.05)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            finally:
                connection.close()
            return None
        with patch("services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint", side_effect=probe) as extract:
            self.assertEqual(self.collect()["jobs_seen"], 1)
        extract.assert_called_once()
        self.assertEqual(self.pending(), [])

    def test_retained_older_winner_cannot_receive_a_new_candidate_hint(self):
        from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint
        from services.company_discovery.requests import record_discovery_requests
        from services.dedupe.source_values import resolve_job_values
        self.collect(profile_id=None)
        with self.store.engine.begin() as connection:
            older = connection.execute(select(m.posting_value_candidates.c.id)).scalar_one()
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source_id).values(last_attempt=None))
        self.rows[0]["description"] += "<p>New source observation.</p>"
        def retain(connection, job_id, **kwargs):
            job = resolve_job_values(connection, job_id, **kwargs)
            attrs = dict(job["attributes"])
            attrs["source_resolution"] = dict(attrs["source_resolution"], selected_candidate_id=older)
            connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(attributes=attrs))
            return job
        with patch("services.discovery.sources.collect.resolve_job_values", side_effect=retain), patch(
                "services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint", wraps=extract_bootstrap_hint) as extract, patch(
                "services.discovery.sources.collect.record_discovery_requests", wraps=record_discovery_requests) as admit:
            self.collect()
        extract.assert_called_once()
        self.assertEqual(admit.call_args.kwargs.get("aggregator_bootstrap_candidates", {}), {})
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.count("posting_snapshots"), 2)

    def test_current_parent_identity_and_attempt_are_forwarded_without_changing_operation(self):
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source_id).values(last_attempt=None))
                lineage = self.parent_operation(scheduled=scheduled)
                with patch("services.discovery.sources.collect.record_discovery_requests") as admit:
                    self.assertEqual(self.collect(**lineage)["jobs_seen"], 1)
                self.assertEqual(admit.call_args.kwargs["parent_operation_id"], lineage["parent_operation_id"])
                self.assertEqual(admit.call_args.kwargs["parent_operation_attempt"], lineage["parent_operation_attempt"])
                self.assertTrue(admit.call_args.kwargs["aggregator_bootstrap_candidates"])
                with self.store.engine.begin() as connection:
                    row = connection.execute(select(m.operations).where(
                        m.operations.c.id == lineage["parent_operation_id"])).mappings().one()
                    self.assertEqual((row["state"], row["attempt_count"]), ("RUNNING", lineage["parent_operation_attempt"]))
                    # End this fixture's parent so the next subcase can claim its own slot.
                    connection.execute(update(m.operations).where(m.operations.c.id == row["id"]).values(state="COMPLETED"))

    def test_direct_collection_does_not_claim_parent_lineage(self):
        with patch("services.discovery.sources.collect.record_discovery_requests") as admit:
            self.assertEqual(self.collect()["jobs_seen"], 1)
        self.assertTrue(admit.call_args.kwargs["aggregator_bootstrap_candidates"])
        self.assertNotIn("parent_operation_id", admit.call_args.kwargs)
        self.assertNotIn("parent_operation_attempt", admit.call_args.kwargs)

    def test_missing_or_malformed_parent_pair_is_rejected_before_feed_read(self):
        cases = [{"parent_operation_id": "missing"}, {"parent_operation_attempt": 1},
                 {"parent_operation_id": "missing", "parent_operation_attempt": True},
                 {"parent_operation_id": "missing", "parent_operation_attempt": 0},
                 {"parent_operation_id": "missing", "parent_operation_attempt": 1}]
        for lineage in cases:
            with self.subTest(lineage=lineage), self.assertRaisesRegex(SourceError, "SOURCE_CHANGED"):
                self.collect(**lineage)
        self.assertEqual(self.http_requests, [])
        self.assertEqual(self.store.count("posting_snapshots"), 0)

    def test_foreign_stale_or_cancelled_parent_is_rejected_before_feed_read(self):
        lineage = self.parent_operation()
        with self.store.engine.connect() as connection:
            original = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == lineage["parent_operation_id"])).mappings().one())
        cases = [{"kind": "COMPANY_DISCOVER"}, {"entity_id": "other-profile"},
                 {"state": "COMPLETED"}, {"cancel_requested": True},
                 {"attempt_count": lineage["parent_operation_attempt"] + 1},
                 {"lease_expires_at": None}, {"lease_expires_at": "2000-01-01T00:00:00+00:00"},
                 {"lease_expires_at": "not-a-date"}, {"lease_expires_at": "2999-01-01T00:00:00"},
                 {"params": dict(original["params"], source_id="other-source")},
                 {"params": dict(original["params"], profile_version="old-version")},
                 {"params": dict(original["params"], profile_ids=["other-profile"])},
                 {"params": dict(original["params"], rescore_only=True)}]
        for changed in cases:
            with self.subTest(changed=changed):
                values = {key: value for key, value in original.items() if key != "id"}
                values.update(changed)
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == original["id"]).values(**values))
                with self.assertRaisesRegex(SourceError, "SOURCE_CHANGED"):
                    self.collect(**lineage)
        self.assertEqual(self.http_requests, [])
        self.assertEqual(self.store.count("posting_snapshots"), 0)

    def test_parent_attempt_change_during_feed_rolls_back_intake(self):
        lineage = self.parent_operation()
        def change_attempt():
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == lineage["parent_operation_id"])
                    .values(attempt_count=lineage["parent_operation_attempt"] + 1))
        self.after_feed_read = change_attempt
        with self.assertRaisesRegex(SourceError, "SOURCE_CHANGED"):
            self.collect(**lineage)
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(self.store.count("posting_snapshots"), 0)
        self.assertEqual(self.pending(), [])
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == self.source_id)).mappings().one()
        self.assertEqual(source["consecutive_failures"], 0)
        self.assertNotEqual(source["health_status"], "COLLECTING")

    def test_scheduled_parent_profile_change_during_feed_rolls_back_intake(self):
        lineage = self.parent_operation(scheduled=True)
        def change_profile():
            updated = deepcopy(PROFILE)
            updated["name"] = "Changed during intake"
            self.store.save_profile(updated)
        self.after_feed_read = change_profile
        with self.assertRaisesRegex(SourceError, "SOURCE_CHANGED"):
            self.collect(**lineage)
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(self.store.count("posting_snapshots"), 0)
        self.assertEqual(self.pending(), [])
