"""Collection stages bounded untrusted hints; employer network belongs to workers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, now
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

    def _activate_v2(self):
        current = self.store.profile('fixture')
        profile = deepcopy(PROFILE)
        profile['preferences'] = deepcopy(current['preferences'])
        expected = current['version']
        profile['preferences'].setdefault('company_discovery', {}).update(
            company_discovery_enabled=True,
            employer_verification=dict(version=1, mode='GREENHOUSE_RECIPROCAL_V1', activation_id=None))
        self.store.save_profile(profile, expected_version=expected)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == self.source_id).values(rate_limit=1))
        self.rows[0]['description'] += '<a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a>'
        return self.store.profile('fixture')

    def _stage_v2(self):
        self._activate_v2()
        lineage = self.parent_operation()
        self.collect(**lineage)
        request = self.pending()[0]
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == request['operation_id'])).mappings().one())
        return lineage, request, operation

    def test_v2_intake_binds_current_activation_actual_parent_and_unchanged_collector(self):
        from packages.domain.employer_verification import verification_policy_snapshot
        from services.company_discovery.aggregator_bootstrap import canonical_digest
        from services.company_discovery.requests import authorize_discovery_request

        lineage, request, operation = self._stage_v2()
        intent = request['domain_evidence'][0]
        binding = intent['binding']
        with self.store.engine.connect() as connection:
            parent = connection.execute(select(m.operations).where(
                m.operations.c.id == lineage['parent_operation_id'])).mappings().one()
        self.assertEqual((intent['schema'], intent['version']), ('AGGREGATOR_BOOTSTRAP_PENDING_V2', 2))
        self.assertEqual(binding['parent_request_id'], parent['params']['request_id'])
        self.assertEqual(binding['parent_operation_attempt'], parent['attempt_count'])
        self.assertEqual(binding['verification_policy'], verification_policy_snapshot(self.store.profile('fixture')))
        self.assertEqual(binding['verification_policy_sha256'], canonical_digest(binding['verification_policy']))
        self.assertEqual(binding['source_policy']['rate_limit'], 1)
        self.assertEqual(binding['source_policy_sha256'], canonical_digest(binding['source_policy']))
        self.assertEqual(operation['params']['aggregator_bootstrap'], 'AGGREGATOR_BOOTSTRAP_PENDING_V2')
        self.assertEqual(operation['params']['fixture_aggregator_bootstrap'], 'fixture-aggregator-bootstrap-v2')
        self.assertNotIn('verification_policy', operation['params'])
        self.assertEqual(authorize_discovery_request(self.store, operation)['aggregator_bootstrap_pending'], intent)
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_v2_active_preference_without_a_parent_cannot_downgrade_to_v1(self):
        self._activate_v2()
        self.assertEqual(self.collect()['jobs_seen'], 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.count('worker_operations'), 0)

    def test_v2_employer_hint_is_reconstructed_from_exact_persisted_intake(self):
        from packages.domain.employer_verification import verification_policy_snapshot
        from services.company_discovery.aggregator_bootstrap import canonical_digest, extract_bootstrap_hint
        from services.company_discovery.requests import authorize_discovery_request, load_aggregator_bootstrap_context

        self._activate_v2()
        employer = 'https://acme.example/jobs/1001'
        self.rows[0]['description'] = '<p>Python development role.</p>' + f'<a href="{employer}">Apply</a>'
        raw = deepcopy(self.rows[0])
        lineage = self.parent_operation()
        self.assertEqual(self.collect(**lineage)['jobs_seen'], 1)
        self.assertEqual(len(self.pending()), 1)
        request = self.pending()[0]
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        with reopened.engine.connect() as connection:
            joined = connection.execute(select(
                m.posting_value_candidates.c.id.label('candidate_id'),
                m.posting_value_candidates.c.normalized,
                m.posting_value_candidates.c.application_url,
                m.posting_snapshots.c.id.label('snapshot_id'),
                m.posting_snapshots.c.raw, m.posting_snapshots.c.sha256,
                m.postings.c.job_id, m.jobs.c.company_id,
                m.company_sources.c.id.label('company_source_id'),
                m.company_sources.c.collector_source_id,
            ).select_from(m.posting_value_candidates)
                .join(m.posting_snapshots, m.posting_snapshots.c.id == m.posting_value_candidates.c.posting_snapshot_id)
                .join(m.postings, m.postings.c.id == m.posting_value_candidates.c.posting_id)
                .join(m.jobs, m.jobs.c.id == m.postings.c.job_id)
                .join(m.company_sources, m.company_sources.c.id == m.posting_value_candidates.c.company_source_id)
                .where(m.posting_value_candidates.c.id == request['candidate_id'])).mappings().one()
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == request['operation_id'])).mappings().one())
            parent = connection.execute(select(m.operations).where(
                m.operations.c.id == lineage['parent_operation_id'])).mappings().one()
        with patch.object(PublicCompanyClient, '_request_once', side_effect=AssertionError('No employer network in admission')):
            authorization = authorize_discovery_request(reopened, operation)
            context = load_aggregator_bootstrap_context(reopened, operation)
        pending = request['domain_evidence'][0]
        binding, hint = pending['binding'], pending['hint']
        normalized = dict(joined['normalized'], application_url=joined['application_url'])
        self.assertEqual((pending['schema'], pending['version']), ('AGGREGATOR_BOOTSTRAP_PENDING_V2', 2))
        self.assertEqual((hint['candidate_kind'], hint['candidate_url']), ('EMPLOYER_PAGE', employer))
        self.assertEqual(hint, extract_bootstrap_hint(joined['raw'], normalized))
        self.assertEqual(joined['raw'], raw)
        self.assertEqual(binding['raw_sha256'], canonical_digest(raw))
        self.assertEqual(joined['sha256'], binding['raw_sha256'])
        for bound, observed in [('candidate_id', 'candidate_id'), ('posting_snapshot_id', 'snapshot_id'),
                                ('trigger_job_id', 'job_id'), ('company_id', 'company_id'),
                                ('company_source_id', 'company_source_id'), ('source_id', 'collector_source_id')]:
            self.assertEqual(binding[bound], joined[observed])
        self.assertEqual((binding['parent_operation_id'], binding['parent_operation_attempt'], binding['parent_request_id']),
                         (parent['id'], parent['attempt_count'], parent['params']['request_id']))
        self.assertEqual(binding['verification_policy'], verification_policy_snapshot(reopened.profile('fixture')))
        self.assertEqual(binding['source_policy']['rate_limit'], 1)
        self.assertEqual(authorization['aggregator_bootstrap_pending'], pending)
        self.assertEqual(context['pending_intent'], pending)
        self.assertEqual(context['hint'], hint)
        self.assertEqual(context['raw'], raw)
        self.assertEqual(context['source_policy']['rate_limit'], 1)
        self.assertEqual((operation['params']['aggregator_bootstrap'], operation['params']['aggregator_bootstrap_version']),
                         ('AGGREGATOR_BOOTSTRAP_PENDING_V2', 2))
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(reopened.count('company_domains'), 0)
        self.assertEqual(reopened.count('applications'), 0)

    def test_v2_missing_actual_parent_request_id_cannot_stage_authority(self):
        self._activate_v2()
        lineage = self.parent_operation()
        with self.store.engine.begin() as connection:
            parent = connection.execute(select(m.operations).where(
                m.operations.c.id == lineage['parent_operation_id'])).mappings().one()
            params = dict(parent['params'])
            params.pop('request_id')
            connection.execute(update(m.operations).where(
                m.operations.c.id == parent['id']).values(params=params))
        self.collect(**lineage)
        self.assertEqual(self.pending(), [])

    def test_v2_disable_reenable_rotates_authority_and_cannot_revive_pending(self):
        from services.company_discovery.public_client import PublicFetchError
        from services.company_discovery.requests import authorize_discovery_request

        _, request, operation = self._stage_v2()
        old = request['domain_evidence'][0]['binding']['verification_policy']['activation_id']
        profile = deepcopy(self.store.profile('fixture'))
        profile['preferences']['company_discovery']['employer_verification'] = {
            'version': 1, 'mode': 'DISABLED', 'activation_id': None}
        self.store.save_profile(profile, expected_version=profile['version'])
        with self.assertRaises(PublicFetchError):
            authorize_discovery_request(self.store, operation)
        profile = deepcopy(self.store.profile('fixture'))
        profile['preferences']['company_discovery']['employer_verification']['mode'] = 'GREENHOUSE_RECIPROCAL_V1'
        self.store.save_profile(profile, expected_version=profile['version'])
        current = self.store.profile('fixture')['preferences']['company_discovery']['employer_verification']
        self.assertNotEqual(current['activation_id'], old)
        with self.assertRaises(PublicFetchError):
            authorize_discovery_request(self.store, operation)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_v2_ordinary_active_edit_then_revert_uses_current_content_semantics(self):
        from services.company_discovery.public_client import PublicFetchError
        from services.company_discovery.requests import authorize_discovery_request

        _, request, operation = self._stage_v2()
        original_version = self.store.profile('fixture')['version']
        original = deepcopy(PROFILE)
        original['preferences'] = deepcopy(self.store.profile('fixture')['preferences'])
        edited = deepcopy(original)
        edited['name'] += ' edited'
        self.store.save_profile(edited, expected_version=original_version)
        with self.assertRaises(PublicFetchError):
            authorize_discovery_request(self.store, operation)
        self.store.save_profile(original, expected_version=self.store.profile('fixture')['version'])
        self.assertEqual(self.store.profile('fixture')['version'], original_version)
        self.assertEqual(authorize_discovery_request(self.store, operation)['aggregator_bootstrap_pending'],
                         request['domain_evidence'][0])

    def test_v2_mixed_markers_overrides_and_typed_version_changes_are_rejected(self):
        from services.company_discovery.public_client import PublicFetchError
        from services.company_discovery.requests import authorize_discovery_request

        _, _, operation = self._stage_v2()
        variants = [('aggregator_bootstrap', 'AGGREGATOR_BOOTSTRAP_PENDING_V1'),
            ('aggregator_bootstrap_version', True), ('aggregator_bootstrap_version', '2'),
            ('fixture_aggregator_bootstrap', 'fixture-aggregator-bootstrap-v1'),
            ('verification_policy', {}), ('verification_policy_override', {})]
        for key, value in variants:
            with self.subTest(field=key, value=value):
                candidate = deepcopy(operation)
                candidate['params'][key] = value
                with self.assertRaises(PublicFetchError):
                    authorize_discovery_request(self.store, candidate)

    def test_v2_parent_request_identity_drift_is_not_accepted(self):
        from services.company_discovery.public_client import PublicFetchError
        from services.company_discovery.requests import authorize_discovery_request

        lineage, _, operation = self._stage_v2()
        with self.store.engine.begin() as connection:
            parent = connection.execute(select(m.operations).where(
                m.operations.c.id == lineage['parent_operation_id'])).mappings().one()
            connection.execute(update(m.operations).where(m.operations.c.id == parent['id']).values(
                params={**parent['params'], 'request_id': 'foreign-parent-request'}))
        with self.assertRaises(PublicFetchError):
            authorize_discovery_request(self.store, operation)

    def test_collection_stages_exact_candidate_without_granting_employer_authority(self):
        self._activate_v2()
        original = deepcopy(self.rows[0])
        lineage = self.parent_operation()
        result = self.collect(**lineage)
        self.assertEqual(result["jobs_seen"], 1)
        self.assertEqual(len(self.http_requests), 1)
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        request = pending[0]
        self.assertEqual(request["profile_id"], "fixture")
        self.assertEqual(request["profile_version"], self.store.profile("fixture")["version"])
        self.assertEqual(request["state"], "QUEUED")
        self.assertEqual(request['domain_evidence'][0]['version'], 2)
        self.assertEqual(request['domain_evidence'][0]['binding']['parent_operation_id'],
                         lineage['parent_operation_id'])
        self.assertEqual(request['domain_evidence'][0]['binding']['parent_operation_attempt'],
                         lineage['parent_operation_attempt'])
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
        self.assertEqual(operation["params"]["aggregator_bootstrap"], "AGGREGATOR_BOOTSTRAP_PENDING_V2")
        self.assertEqual([row["provider"] for row in sources], ["remotive"])
        self.assertEqual(reopened.count("company_domains"), 0)
        self.assertEqual(reopened.count("applications"), 0)

    def test_cap_is_two_distinct_employers_without_truncating_collected_jobs(self):
        self._activate_v2()
        template = self.rows[0]
        self.rows = []
        for index, company in enumerate(("Alpha", "Alpha", "Bravo", "Charlie", "Delta")):
            identifier = 2000 + index
            self.rows.append(dict(template, id=identifier, company_name=company,
                title=f"Python Engineer {index}",
                url=f"https://remotive.com/remote-jobs/software-dev/python-engineer-{identifier}"))
        result = self.collect(**self.parent_operation())
        self.assertEqual(result["jobs_seen"], 5)
        self.assertEqual(self.store.count("posting_snapshots"), 5)
        pending = self.pending()
        self.assertEqual(len(pending), 2)
        self.assertEqual(len({row["company_id"] for row in pending}), 2)
        self.assertEqual(len(self.http_requests), 1)

    def _stage_matching_bootstrap_heads(self):
        self._activate_v2()
        template = deepcopy(self.rows[0])
        self.rows = [dict(template, id=5000 + index, company_name=company,
            title="Python SQL Engineer", tags=["python", "sql"],
            description='Python and SQL engineering. <a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a>',
            url=f"https://remotive.com/remote-jobs/software-dev/python-engineer-{5000 + index}")
            for index, company in enumerate(("Alpha", "Bravo", "Charlie", "Delta"))]
        lineage = self.parent_operation()
        self.collect(**lineage)
        with self.store.engine.begin() as connection:
            scores = connection.execute(select(m.matches.c.score).where(
                m.matches.c.profile_id == "fixture")).scalars().all()
            self.assertEqual(len(scores), 4)
            self.assertTrue(all(score >= 60 for score in scores), scores)
            connection.execute(update(m.operations).where(
                m.operations.c.id == lineage["parent_operation_id"]).values(
                    state="COMPLETED", completed_at=now(), lease_expires_at=None))
        heads = self.pending()
        self.assertEqual(len(heads), 2)
        return heads

    def _finish_bootstrap_heads(self, heads):
        for request in heads:
            self.assertIsNotNone(claim(self.store, request["operation_id"]))
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == request["operation_id"]).values(
                        state="BLOCKED", error="COMPANY_DOMAIN_CORROBORATION_REQUIRED",
                        completed_at=now(), lease_expires_at=None, next_attempt_at=None))

    def test_terminal_bootstrap_heads_yield_to_next_two_matching_employers(self):
        from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint

        heads = self._stage_matching_bootstrap_heads()
        self._finish_bootstrap_heads(heads)
        with self.store.engine.begin() as connection:
            operations_before = {row["id"]: dict(row) for row in connection.execute(
                select(m.operations).where(m.operations.c.id.in_(
                    [request["operation_id"] for request in heads]))).mappings()}
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == self.source_id).values(last_attempt=None))
        with patch("services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint",
                   wraps=extract_bootstrap_hint) as extract:
            result = self.collect(**self.parent_operation())

        self.assertEqual(result["jobs_seen"], 4)
        self.assertEqual([call.args[0]["company_name"] for call in extract.call_args_list],
                         ["Charlie", "Delta"])
        after = self.pending()
        self.assertEqual(len(after), 4)
        self.assertEqual(len(self.http_requests), 2)
        with self.store.engine.connect() as connection:
            for old in heads:
                current = next(row for row in after if row["id"] == old["id"])
                for key in ("revision", "candidate_id", "trigger_job_id", "source_snapshot",
                            "authorization", "selected_website", "domain_evidence", "operation_id"):
                    self.assertEqual(current[key], old[key], key)
                operation = dict(connection.execute(select(m.operations).where(
                    m.operations.c.id == old["operation_id"])).mappings().one())
                self.assertEqual(operation, operations_before[operation["id"]])

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
        self._activate_v2()
        self.rows[0]["aggregator_bootstrap_candidate"] = {
            "schema": "AGGREGATOR_BOOTSTRAP_HINT_V1", "candidate_url": "https://attacker.example/apply"}
        self.rows[0]["domain_evidence"] = {
            "schema": "AGGREGATOR_NATIVE_BOOTSTRAP_V1", "verified": True,
            "employer_url": "https://attacker.example/apply"}
        original = deepcopy(self.rows[0])
        self.collect(**self.parent_operation())
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

    def test_employer_verification_opt_out_does_not_route_aggregator_through_generic_discovery(self):
        profile = deepcopy(PROFILE)
        profile["preferences"] = {"company_discovery": {
            "company_discovery_enabled": True,
            "employer_verification": {"version": 1, "mode": "DISABLED", "activation_id": None},
        }}
        self.store.save_profile(profile)
        self.rows[0]["description"] += (
            '<a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a>'
        )

        self.assertEqual(self.collect(**self.parent_operation())["jobs_seen"], 1)

        self.assertEqual(self.pending(), [])
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.company_discovery_requests)).all(), [])
            self.assertEqual(connection.execute(select(m.operations.c.id).where(
                m.operations.c.kind == "COMPANY_DISCOVER")).all(), [])
        self.assertEqual(self.store.count("company_domains"), 0)

    def test_missing_employer_verification_policy_does_not_stage_aggregator(self):
        profile = deepcopy(PROFILE)
        profile["preferences"] = {"company_discovery": {"company_discovery_enabled": True}}
        self.store.save_profile(profile)
        self.rows[0]["description"] += (
            '<a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a>'
        )

        self.assertEqual(self.collect(**self.parent_operation())["jobs_seen"], 1)

        self.assertEqual(self.pending(), [])
        self.assertEqual(self.store.count("company_discovery_requests"), 0)

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


@pytest.mark.parametrize("change", [
    "queued", "running", "retryable", "unattempted", "cancelled", "cancel_requested",
    "no_completion", "leased", "review", "foreign_profile", "profile_version",
    "source_policy", "pending_digest", "parent_authority", "incoming_raw",
])
def test_bootstrap_continuation_preserves_nonconsumed_or_changed_heads(change):
    from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint

    case = AggregatorBootstrapCollectionTests()
    case.setUp()
    try:
        heads = case._stage_matching_bootstrap_heads()
        case._finish_bootstrap_heads(heads)
        identifiers = [row["operation_id"] for row in heads]
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with case.store.engine.begin() as connection:
            operations = m.operations.c.id.in_(identifiers)
            if change == "queued":
                connection.execute(update(m.operations).where(operations).values(
                    state="QUEUED", completed_at=None))
            elif change == "running":
                connection.execute(update(m.operations).where(operations).values(
                    state="RUNNING", completed_at=None, lease_expires_at=future))
            elif change == "retryable":
                connection.execute(update(m.operations).where(operations).values(
                    state="FAILED", next_attempt_at=future))
            elif change == "unattempted":
                connection.execute(update(m.operations).where(operations).values(attempt_count=0))
            elif change == "cancelled":
                connection.execute(update(m.operations).where(operations).values(state="CANCELLED"))
            elif change == "cancel_requested":
                connection.execute(update(m.operations).where(operations).values(cancel_requested=True))
            elif change == "no_completion":
                connection.execute(update(m.operations).where(operations).values(completed_at=None))
            elif change == "leased":
                connection.execute(update(m.operations).where(operations).values(lease_expires_at=future))
            elif change == "review":
                connection.execute(update(m.company_discovery_requests).where(
                    m.company_discovery_requests.c.id.in_([row["id"] for row in heads])).values(
                        state="NEEDS_ORIGIN_REVIEW", operation_id=None, authorization=None,
                        selected_website=None, domain_evidence=[]))
            elif change == "source_policy":
                connection.execute(update(m.job_sources).where(
                    m.job_sources.c.id == case.source_id).values(rate_limit=2))
            elif change == "pending_digest":
                for row in heads:
                    evidence = deepcopy(row["domain_evidence"])
                    evidence[0]["sha256"] = "0" * 64
                    connection.execute(update(m.company_discovery_requests).where(
                        m.company_discovery_requests.c.id == row["id"]).values(domain_evidence=evidence))
            elif change == "parent_authority":
                parent_ids = {row["domain_evidence"][0]["binding"]["parent_operation_id"] for row in heads}
                connection.execute(update(m.operations).where(
                    m.operations.c.id.in_(parent_ids)).values(state="FAILED"))
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == case.source_id).values(last_attempt=None))
        if change == "foreign_profile":
            other = deepcopy(PROFILE)
            other["id"] = "other"
            case.store.save_profile(other)
            with case.store.engine.begin() as connection:
                connection.execute(update(m.company_discovery_requests).where(
                    m.company_discovery_requests.c.id.in_([row["id"] for row in heads])).values(
                        profile_id="other", profile_version=case.store.profile("other")["version"]))
        elif change == "profile_version":
            current = case.store.profile("fixture")
            current["name"] = "Changed profile version"
            case.store.save_profile(current, expected_version=current["version"])
        elif change == "incoming_raw":
            case.rows[0]["description"] += " Updated employer posting content."

        with patch("services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint",
                   wraps=extract_bootstrap_hint) as extract:
            case.collect(**case.parent_operation())
        expected = ["Alpha", "Charlie"] if change == "incoming_raw" else ["Alpha", "Bravo"]
        assert [call.args[0]["company_name"] for call in extract.call_args_list] == expected
        assert len(case.http_requests) == 2
    finally:
        case.doCleanups()
