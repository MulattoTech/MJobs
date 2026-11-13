"""Focused, offline validation for parent-bound source containment."""

import hashlib
import json
import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import httpx

from cli.acceptance_source_containment import (
    CHILD_DYNAMIC,
    CHILD_FIELDS,
    MAX_CHILD_SNAPSHOT_ATTEMPTS,
    ContainmentError,
    OwnedPublicSourceContainment,
)
from cli.acceptance_public_source import FINGERPRINT_FIELDS, child_containment_context, run as run_public_source
from tests.unit import test_acceptance_containment as containment_fixtures


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def child_row(**overrides):
    row = {
        "request_id": "request-1",
        "revision": 1,
        "company_id": "company-1",
        "company_source_id": "company-source-1",
        "profile_id": "profile-1",
        "profile_version": "profile-v1",
        "source_id": "primary-source",
        "parent_operation_id": "parent-op",
        "parent_operation_attempt": 1,
        "trigger_job_id": "job-1",
        "candidate_id": "candidate-1",
        "posting_id": "posting-1",
        "snapshot_id": "snapshot-1",
        "snapshot_sha256": "a" * 64,
        "quality_observation_id": "quality-1",
        "operation_id": "child-op-1",
        "state": "BLOCKED",
        "authorization": "AUTHORIZED",
        "reason": "ROBOTS_BLOCKED",
        "operation_attempt": 1,
        "operation_sha256": "b" * 64,
        "pending_sha256": "c" * 64,
    }
    row.update(overrides)
    return row


def source_policy(source_id="collector-gh"):
    return {
        "id": source_id,
        "provider": "greenhouse",
        "tenant": "default",
        "enabled": True,
        "poll_interval": 86400,
        "concurrency": 1,
        "request_timeout": 10,
        "rate_limit": 1,
        "jitter_seconds": 0,
        "strategy": "PUBLIC_JSON",
        "capabilities": {"direct_application": False},
    }


def enrolled_source(row=None):
    row = row or child_row()
    policy = source_policy()
    return {
        "source_id": policy["id"],
        "company_source_id": "company-source-1",
        "operation_id": row["operation_id"],
        "request_id": row["request_id"],
        "policy": policy,
        "policy_sha256": digest({key: policy[key] for key in FINGERPRINT_FIELDS}),
        "audit_id": "audit-1",
        "audit_sha256": "d" * 64,
        "pending_sha256": row["pending_sha256"],
        "receipt_sha256": "e" * 64,
        "envelope_sha256": "f" * 64,
    }


def response(children=None, enrolled=None, *, complete=True):
    rows = children if children is not None else [child_row()]
    result = {
        "schema_version": 1,
        "profile_id": "profile-1",
        "profile_version": "profile-v1",
        "source_id": "primary-source",
        "parent_operation_id": "parent-op",
        "parent_operation_attempt": 1,
        "parent_request_id": "parent-request",
        "complete": complete,
        "limit": 2,
        "children": rows,
        "enrolled_sources": enrolled if enrolled is not None else [],
    }
    stable = [
        {key: row[key] for key in CHILD_FIELDS if key not in CHILD_DYNAMIC}
        for row in sorted(rows, key=lambda item: item["request_id"])
    ]
    result["lineage_sha256"] = digest(stable)
    result["sha256"] = digest(result)
    return result


class ChildReadbackTests(unittest.TestCase):
    def setUp(self):
        self.owner = object.__new__(OwnedPublicSourceContainment)

    def validate(self, value, *, require_complete=True):
        return self.owner._validate_children_response(
            value,
            profile_id="profile-1",
            profile_version="profile-v1",
            source_id="primary-source",
            parent_id="parent-op",
            parent_attempt=1,
            parent_request_id="parent-request",
            require_complete=require_complete,
            expected_parent_state="COMPLETED" if require_complete else "RUNNING",
        )

    def test_exact_parent_family_and_terminal_blocked_child_are_accepted_for_cleanup(self):
        value = response(enrolled=[enrolled_source()])
        checked = self.validate(value)
        self.assertEqual(["child-op-1"], checked["operation_ids"])
        self.assertEqual(["request-1"], checked["request_ids"])
        self.assertTrue(OwnedPublicSourceContainment._children_terminal(checked["children"]))
        self.assertFalse(checked["acceptance_success"])

    def test_complete_empty_family_is_not_run_not_successful_and_remains_safe_to_cleanup(self):
        checked = self.validate(response(children=[], enrolled=[]))
        self.assertEqual("NOT_RUN", checked["acceptance_result"])
        self.assertFalse(checked["acceptance_success"])
        self.assertTrue(OwnedPublicSourceContainment._children_terminal(checked["children"]))

    def test_running_parent_is_readable_but_not_a_frozen_cleanup_lineage(self):
        value = response(children=[child_row(state="RUNNING")], complete=False)
        checked = self.validate(value, require_complete=False)
        self.assertFalse(checked["complete"])
        self.assertFalse(OwnedPublicSourceContainment._children_terminal(checked["children"]))

    def test_wrong_identity_or_bad_readback_digest_fails_closed(self):
        changed = response()
        changed["children"][0]["parent_operation_id"] = "other-parent"
        changed["sha256"] = digest({key: val for key, val in changed.items() if key != "sha256"})
        with self.assertRaises(ContainmentError):
            self.validate(changed)

        changed = response()
        changed["sha256"] = "0" * 64
        with self.assertRaises(ContainmentError):
            self.validate(changed)

    def test_incomplete_response_cannot_be_used_as_frozen_cleanup_lineage(self):
        with self.assertRaises(ContainmentError):
            self.validate(response(complete=False), require_complete=True)

    def test_more_than_two_or_duplicate_children_are_rejected(self):
        with self.assertRaises(ContainmentError):
            self.validate(response([child_row(), child_row(request_id="request-2"), child_row(request_id="request-3")]))
        with self.assertRaises(ContainmentError):
            self.validate(response([child_row(), child_row()]))

    def test_enrollment_must_join_exact_child_and_policy(self):
        enrolled = enrolled_source()
        with self.assertRaises(ContainmentError):
            self.validate(response(enrolled=[{**enrolled, "request_id": "unrelated"}]))
        wrong_policy = deepcopy(enrolled)
        wrong_policy["policy"]["rate_limit"] = 4
        with self.assertRaises(ContainmentError):
            self.validate(response(enrolled=[wrong_policy]))

    def test_only_explicit_terminal_states_allow_cleanup(self):
        for terminal in ("FAILED", "BLOCKED", "CANCELLED", "DEAD_LETTERED", "DISCARDED"):
            self.assertTrue(OwnedPublicSourceContainment._children_terminal([child_row(state=terminal)]))
        for terminal in ("NEEDS_ORIGIN_REVIEW", "DISMISSED"):
            self.assertTrue(OwnedPublicSourceContainment._children_terminal([child_row(state=terminal, operation_id=None)]))
        self.assertFalse(OwnedPublicSourceContainment._children_terminal([child_row(state="RUNNING")]))


class _PublicSourceFixture:
    setUp = containment_fixtures.OwnedPublicContainmentTests.setUp
    _sleep = containment_fixtures.OwnedPublicContainmentTests._sleep
    _scheduler_health_lag = containment_fixtures.OwnedPublicContainmentTests._scheduler_health_lag
    _full_health = containment_fixtures.OwnedPublicContainmentTests._full_health
    _exercise_public_source_helper = containment_fixtures.OwnedPublicContainmentTests._exercise_public_source_helper
    context = staticmethod(containment_fixtures.OwnedPublicContainmentTests.context)

    def containment(self, **kwargs):
        return containment_fixtures.OwnedPublicContainmentTests.containment(
            self, containment_class=OwnedPublicSourceContainment, **kwargs
        )


class PublicSourcePauseHealthTests(_PublicSourceFixture, unittest.TestCase):
    """The real subclass must reach its child inventory after its owned pause."""

    def test_scheduler_only_503_reaches_child_inventory_without_source_writes(self):
        self.docker.on_pause = self._scheduler_health_lag
        owner = self.containment()
        proof = owner("preflight", self.context(provider="remotive"))
        self.assertTrue(proof["scheduler_contained"])
        self.assertEqual(owner._initial_all_sources, {})
        self.assertEqual(owner._initial_company_sources, {})
        self.assertIn(("GET", "/company-sources", {}), self.api.calls)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))
        self.assertEqual(self.docker.pause_calls, ["b" * 64])
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(owner.claim_path.exists())

    def test_dependency_failure_after_pause_never_reaches_child_inventory(self):
        def pause():
            self._scheduler_health_lag()
            self.api.ready["checks"]["workers"]["mjobs-company-discovery"] = False

        self.docker.on_pause = pause
        owner = self.containment()
        with self.assertRaisesRegex(ContainmentError, "FIXTURE_API_DEPENDENCY_UNHEALTHY"):
            owner("preflight", self.context(provider="remotive"))
        self.assertIsNone(owner._initial_all_sources)
        self.assertIsNone(owner._initial_company_sources)
        self.assertFalse(any(path == "/company-sources" for _, path, _ in self.api.calls))
        self.assertTrue(owner.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, [])

    def test_preexisting_unhealthy_scheduler_is_rejected_before_child_inventory(self):
        self.docker.rows["scheduler"]["health"] = "unhealthy"
        owner = self.containment()
        with self.assertRaisesRegex(ContainmentError, "PROJECT_CONTAINER_UNHEALTHY"):
            owner("preflight", self.context(provider="remotive"))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.docker.pause_calls, [])
        self.assertFalse(owner.claim_path.exists())

    def test_public_helper_keeps_child_cleanup_and_release_gates_during_health_lag(self):
        self.docker.on_pause = self._scheduler_health_lag
        self.docker.on_unpause = self._full_health
        self._exercise_public_source_helper()


class ChildContextInitializationTests(_PublicSourceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.containment()
        self.owner._last_stage = "read"

    def checked(self, children=None, enrolled=None, *, complete=True):
        return self.owner._validate_children_response(
            response(children=children, enrolled=enrolled, complete=complete),
            profile_id="profile-1", profile_version="profile-v1", source_id="primary-source",
            parent_id="parent-op", parent_attempt=1, parent_request_id="parent-request",
            require_complete=complete, expected_parent_state="COMPLETED" if complete else "RUNNING",
        )

    @staticmethod
    def projection(checked=None):
        return {
            "child_discovery_requested": True,
            "owned_child_operation_ids": [] if checked is None else checked["operation_ids"],
            "owned_child_request_ids": [] if checked is None else checked["request_ids"],
            "owned_child_lineage_sha256": None if checked is None else checked["lineage_sha256"],
            "owned_child_sources": [] if checked is None else deepcopy(checked["enrolled_sources"]),
        }

    def test_empty_family_bootstrap_is_one_bounded_read_then_exact_projection(self):
        checked = self.checked(children=[], enrolled=[])
        bootstrap = self.projection()
        self.owner._validate_child_context(bootstrap, checked)
        self.owner._validate_child_context(bootstrap, checked)
        with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
            self.owner._validate_child_context(bootstrap, checked)
        self.owner._validate_child_context(self.projection(checked), checked)
        with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
            self.owner._validate_child_context(bootstrap, checked)

    def test_nonempty_bootstrap_covers_trigger_reads_without_freezing_dynamic_state(self):
        running = self.checked(children=[child_row(state="RUNNING")], enrolled=[])
        completed = self.checked(children=[child_row(state="COMPLETED", operation_sha256="e" * 64)], enrolled=[])
        self.assertEqual(running["lineage_sha256"], completed["lineage_sha256"])
        for checked in (running, running, completed, completed):
            self.owner._validate_child_context(self.projection(), checked)
        with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
            self.owner._validate_child_context(self.projection(), completed)
        self.owner._validate_child_context(self.projection(completed), completed)

    def test_unobserved_projection_never_authorizes_mutation_cleanup_or_release(self):
        checked = self.checked(children=[], enrolled=[])
        for stage in ("enable", "dispatch", "poll", "finalize", "cleanup", "release"):
            with self.subTest(stage=stage):
                self.owner._last_stage = stage
                with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
                    self.owner._validate_child_context(self.projection(), checked)
        for stage in ("finalize", "cleanup", "release"):
            with self.subTest(unrequested_stage=stage):
                self.owner._last_stage = stage
                context = {**self.projection(), "child_discovery_requested": False}
                with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
                    self.owner._validate_child_context(context, checked)

    def test_bootstrap_requires_completed_parent_and_empty_unforged_projection(self):
        with self.assertRaisesRegex(ContainmentError, "CHILD_READBACK_NOT_FINAL"):
            self.owner._validate_child_context(self.projection(), self.checked(complete=False))
        checked = self.checked()
        for key, value in (("owned_child_operation_ids", ["forged"]),
                           ("owned_child_request_ids", ["forged"]),
                           ("owned_child_sources", [enrolled_source()])):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
                    self.owner._validate_child_context({**self.projection(), key: value}, checked)

    def test_bound_parent_family_and_projected_identity_cannot_change(self):
        checked = self.checked(enrolled=[])
        self.owner._validate_child_context(self.projection(), checked)
        for key in checked["parent_identity"]:
            changed = deepcopy(checked)
            changed["parent_identity"][key] = "foreign"
            with self.subTest(parent_key=key):
                with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
                    self.owner._validate_child_context(self.projection(), changed)
        changed = deepcopy(checked)
        changed["lineage_sha256"] = "f" * 64
        with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
            self.owner._validate_child_context(self.projection(changed), changed)
        self.owner._validate_child_context(self.projection(checked), checked)
        for key, value in (("child_discovery_requested", False),
                           ("owned_child_lineage_sha256", None),
                           ("owned_child_operation_ids", []),
                           ("owned_child_request_ids", [])):
            with self.subTest(projection_key=key):
                with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
                    self.owner._validate_child_context({**self.projection(checked), key: value}, checked)

    def test_async_enrollment_can_be_read_but_requires_full_projection_for_cleanup(self):
        initial = self.checked(children=[child_row(state="RUNNING")], enrolled=[])
        complete = self.checked(children=[child_row(state="COMPLETED")], enrolled=[enrolled_source()])
        initial_projection = self.projection(initial)
        self.owner._validate_child_context(initial_projection, initial)
        for stage in ("read", "poll"):
            self.owner._last_stage = stage
            self.owner._validate_child_context(initial_projection, complete)
        self.owner._last_stage = "cleanup"
        with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
            self.owner._validate_child_context(initial_projection, complete)
        self.owner._validate_child_context(self.projection(complete), complete)
        self.owner._child_frozen = complete
        self.owner._last_stage = "read"
        with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
            self.owner._validate_child_context(initial_projection, complete)

    def test_verified_and_projected_enrollments_cannot_be_removed_or_altered(self):
        complete = self.checked(enrolled=[enrolled_source()])
        projection = self.projection(complete)
        self.owner._validate_child_context(projection, complete)
        for native in (self.checked(enrolled=[]), deepcopy(complete)):
            if native["enrolled_sources"]:
                native["enrolled_sources"][0]["audit_sha256"] = "0" * 64
            with self.assertRaisesRegex(ContainmentError, "CHILD_ENROLLMENT_CHANGED"):
                self.owner._validate_child_context(projection, native)
        for rows in ([], [deepcopy(projection["owned_child_sources"][0])],
                     [projection["owned_child_sources"][0]] * 2):
            if len(rows) == 1:
                rows[0]["receipt_sha256"] = "0" * 64
            with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
                self.owner._validate_child_context({**projection, "owned_child_sources": rows}, complete)


class AsyncChildAPI(containment_fixtures.FakeAPI):
    """An independently read family whose native enrollment arrives after a poll."""

    def __init__(self):
        super().__init__()
        self.collection_state = "COMPLETED"
        self.child = None
        self.enrolled = []
        self.company_sources = []

    def complete_child(self):
        self.child["state"] = "COMPLETED"
        self.child["operation_sha256"] = "e" * 64
        self.operations[1]["state"] = "COMPLETED"
        self.enrolled = [enrolled_source(self.child)]
        self.sources.append(deepcopy(self.enrolled[0]["policy"]))
        self.company_sources.append({
            "id": "company-source-1", "company_id": self.child["company_id"],
            "collector_source_id": "collector-gh", "provider": "greenhouse",
            "tenant": "default", "enabled": True,
        })

    def children_response(self):
        parent = self.operations[0]
        value = response(children=[self.child], enrolled=self.enrolled)
        value.update(profile_id="profile", profile_version="v1", source_id="public-source",
                     parent_operation_id="own-operation", parent_request_id=parent["params"]["request_id"])
        value["sha256"] = digest({key: val for key, val in value.items() if key != "sha256"})
        return value

    def request(self, method, path, **kwargs):
        if method == "GET" and path == "/company-sources":
            self.calls.append((method, path, deepcopy(kwargs)))
            return {"company_sources": deepcopy(self.company_sources)}
        if method == "GET" and path.endswith("/children"):
            self.calls.append((method, path, deepcopy(kwargs)))
            assert kwargs["params"] == {"attempt": 1}
            return deepcopy(self.children_response())
        if method == "GET" and path.endswith("/domain-proofs"):
            # The separate diagnostic-capture slice may consume this after merge.
            child_snapshot = self.children_response()
            value = {key: child_snapshot[key] for key in (
                "schema_version", "profile_id", "profile_version", "source_id", "parent_operation_id",
                "parent_operation_attempt", "parent_request_id", "complete", "limit",
            )}
            value.update(collection_lineage_sha256=child_snapshot["lineage_sha256"],
                         collection_readback_sha256=child_snapshot["sha256"], observations=[{
                "child": deepcopy(self.child), "status": "UNAVAILABLE", "reason": "BOOTSTRAP_RECEIPT_NOT_PERSISTED",
                "proof": None, "enrolled_sources": [{k: v for k, v in item.items() if k != "policy" and k != "policy_sha256"}
                                                    for item in self.enrolled],
            }])
            value["sha256"] = digest(value)
            return value
        if method == "PUT" and path == "/company-sources/company-source-1/enabled":
            self.calls.append((method, path, deepcopy(kwargs)))
            assert kwargs["json"] == {"enabled": False}
            self.company_sources[0]["enabled"] = False
            return deepcopy(self.company_sources[0])
        if method == "PUT" and path == "/sources" and kwargs["json"]["provider"] == "greenhouse":
            self.calls.append((method, path, deepcopy(kwargs)))
            self.sources[1].update(deepcopy(kwargs["json"]))
            return {"id": "collector-gh"}
        result = super().request(method, path, **kwargs)
        if method == "POST" and path == "/sources/public-source/collect":
            self.child = child_row(
                state="RUNNING", profile_id="profile", profile_version="v1", source_id="public-source",
                parent_operation_id="own-operation", company_source_id="aggregator-source",
                trigger_job_id="real-job", posting_id="public-posting", snapshot_id="snapshot",
                snapshot_sha256=self.public["postings"][0]["snapshots"][0]["sha256"],
            )
            self.operations.append({
                "id": self.child["operation_id"], "kind": "COMPANY_DISCOVER", "state": "RUNNING",
                "entity_id": self.child["company_id"], "attempt_count": 1,
                "params": {"discovery_request_id": self.child["request_id"], "discovery_request_revision": 1,
                           "profile_id": "profile", "profile_version": "v1"},
            })
        return result


class IndependentClientChildFlowTests(_PublicSourceFixture, unittest.TestCase):
    def test_independent_client_bootstraps_nonempty_family_and_observes_async_enrollment(self):
        self.api = AsyncChildAPI()
        self.docker.on_pause = self._scheduler_health_lag
        self.docker.on_unpause = self._full_health
        baseline = {
            "source": {"commit": containment_fixtures.REVISION},
            "profile_id": "profile", "application_id": "application",
            "operation_id": "baseline-operation", "request_id": "baseline-request", "profile_version": "v1",
            "selected_application_job_id": "fixture-job", "selected_application_route_id": "fixture-route",
            "confirmation": self.api.app["confirmation"], "external_submission_enabled": False,
        }
        owner = self.containment(baseline=baseline)
        live_reads = []
        real_client = httpx.Client

        def handle(request):
            kwargs = {}
            if request.content:
                kwargs["json"] = json.loads(request.content)
            if request.url.params:
                kwargs["params"] = {k: int(v) if k == "attempt" else v for k, v in request.url.params.items()}
            live_reads.append((request.method, request.url.path))
            result = self.api.request(request.method, request.url.path, **kwargs)
            status = 200
            if isinstance(result, httpx.Response):
                status, result = result.status_code, result.json()
            return httpx.Response(status, json=result)

        def client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return real_client(transport=httpx.MockTransport(handle), **kwargs)

        def finish_child(seconds):
            self._sleep(seconds)
            self.api.complete_child()

        with patch("cli.acceptance_public_source.httpx.Client", side_effect=client):
            evidence = run_public_source(
                owner.base_url, baseline, self.run_dir / "public-independent-client.json",
                acceptance_run_id="independent-client-run", execution_scope="CONTROLLED_TRANSPORT",
                containment_preflight=owner, utc_now=lambda: self.now[0],
                monotonic=lambda: self.clock[0], sleep=finish_child,
            )
        self.assertEqual(evidence["result"], "PASS", evidence)
        self.assertEqual(evidence["acceptance_observations"][0]["status"], "NOT_RUN")
        self.assertEqual(evidence["child_discovery"]["status"], "COMPLETED")
        self.assertEqual(evidence["child_discovery"]["polls"], 2)
        self.assertEqual(len(evidence["child_discovery"]["enrolled_sources"]), 1)
        self.assertEqual(sum(path.endswith("/children") for _, path in live_reads), 2)
        self.assertTrue(owner._child_projection_observed)
        self.assertTrue(all(row["enabled"] is False for row in self.api.sources))
        self.assertFalse(self.api.company_sources[0]["enabled"])
        context = self.context(
            source_id=evidence["source_id"], source_policy_sha256=evidence["source_policy_sha256"],
            newly_created=True, collection_operation_id=evidence["collection_operation_id"],
            collection_request_id=evidence["collection_request_id"], **child_containment_context(evidence),
        )
        owner("finalize", context)
        released = owner("release", {**context, "cleanup_status": evidence["cleanup"]["status"]})
        self.assertTrue(released["released"])
        self.assertFalse(owner.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])


class TransitioningChildAPI(AsyncChildAPI):
    """Each HTTP request returns its own coherent, advancing API snapshot."""

    def __init__(self, transition):
        super().__init__()
        self.transition = transition
        self.collection_state = "RUNNING" if transition == "parent" else "COMPLETED"
        self.transitioned = False

    def children_response(self):
        value = super().children_response()
        value["complete"] = self.operations[0]["state"] == "COMPLETED"
        value["sha256"] = digest({key: val for key, val in value.items() if key != "sha256"})
        return value

    def advance(self):
        self.collection_state = "COMPLETED"
        self.operations[0]["state"] = "COMPLETED"
        self.operations[1]["state"] = "BLOCKED"
        self.child.update(state="BLOCKED", operation_sha256="e" * 64,
                          authorization="AGGREGATOR_BOOTSTRAP_PENDING",
                          reason="COMPANY_DOMAIN_CORROBORATION_REQUIRED")
        self.transitioned = True


class IndependentSnapshotTransitionTests(_PublicSourceFixture, unittest.TestCase):
    """Use distinct real HTTPX clients for the helper and the ownership guard."""

    def exercise_transition(self, transition):
        self.api = TransitioningChildAPI(transition)
        self.docker.on_pause = self._scheduler_health_lag
        self.docker.on_unpause = self._full_health
        baseline = {
            "source": {"commit": containment_fixtures.REVISION},
            "profile_id": "profile", "application_id": "application",
            "operation_id": "baseline-operation", "request_id": "baseline-request",
            "profile_version": "v1", "selected_application_job_id": "fixture-job",
            "selected_application_route_id": "fixture-route",
            "confirmation": self.api.app["confirmation"], "external_submission_enabled": False,
        }
        owner = self.containment(baseline=baseline)
        owner._injected_request = None
        clients, calls, errors = [], [], []
        real_client = httpx.Client

        def client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            self.assertEqual(kwargs["timeout"], containment_fixtures.API_TIMEOUT_SECONDS)
            channel = "helper" if not clients else "owner"

            def handle(request):
                path = request.url.path
                data = {}
                if request.content:
                    data["json"] = json.loads(request.content)
                if request.url.params:
                    data["params"] = {k: int(v) if k == "attempt" else v
                                      for k, v in request.url.params.items()}
                calls.append((channel, request.method, path))
                if channel == "owner" and path.endswith("/children") and not self.api.transitioned:
                    self.api.advance()
                if request.method == "POST" and path == "/operations/own-operation/cancel":
                    return httpx.Response(202, json={"cancel_requested": True})
                result = self.api.request(request.method, path, **data)
                status = 200
                if isinstance(result, httpx.Response):
                    status, result = result.status_code, result.json()
                return httpx.Response(status, json=result)

            current = real_client(transport=httpx.MockTransport(handle), **kwargs)
            clients.append(current)
            return current

        def contain(stage, context):
            try:
                return owner(stage, context)
            except ContainmentError as error:
                errors.append(str(error))
                raise

        with patch("cli.acceptance_containment.httpx.Client", side_effect=client):
            evidence = run_public_source(
                owner.base_url, baseline, self.run_dir / "public-transition.json",
                acceptance_run_id="separate-http-snapshot-transition",
                execution_scope="CONTROLLED_TRANSPORT", containment_preflight=contain,
                utc_now=lambda: self.now[0], monotonic=lambda: self.clock[0], sleep=self._sleep,
            )
            self.assertEqual(evidence["result"], "PASS", {
                "error_code": evidence.get("error_code"), "owner_errors": errors,
                "transition": transition, "final_calls": calls[-12:],
            })
            self.assertEqual(evidence["acceptance_observations"][0]["status"], "NOT_RUN")
            self.assertEqual(evidence["child_discovery"]["status"], "BLOCKED")
            self.assertEqual(evidence["child_discovery"]["children"][0]["state"], "BLOCKED")
            self.assertEqual(evidence["observed_real_job_ids"], ["real-job"])
            self.assertEqual(evidence["cleanup"]["status"], "RESTORED_DISABLED")
            context = self.context(
                source_id=evidence["source_id"], source_policy_sha256=evidence["source_policy_sha256"],
                newly_created=True, collection_operation_id=evidence["collection_operation_id"],
                collection_request_id=evidence["collection_request_id"], **child_containment_context(evidence),
            )
            released = owner("release", {**context, "cleanup_status": evidence["cleanup"]["status"]})
            self.assertTrue(released["released"])
        self.assertFalse(owner.claim_path.exists())
        self.assertTrue(self.api.transitioned)
        self.assertGreater(len(clients), 2)
        self.assertTrue(all(client.is_closed for client in clients))
        self.assertTrue(any(c == "helper" and p.endswith("/children") for c, _, p in calls))
        self.assertTrue(any(c == "owner" and p.endswith("/children") for c, _, p in calls))
        self.assertEqual(sum(m == "POST" and p.endswith("/collect") for _, m, p in calls), 1)
        self.assertFalse(any(p.endswith("/cancel") for _, _, p in calls))
        self.assertEqual(errors, [])

    def test_running_parent_completes_between_operation_and_children_http_snapshots(self):
        self.exercise_transition("parent")

    def test_running_child_completes_between_operation_and_children_http_snapshots(self):
        self.exercise_transition("child")


class OwnedSnapshotReconciliationTests(_PublicSourceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.api = TransitioningChildAPI("child")
        self.owner = self.containment()
        self.owner._injected_request = None
        self.http_calls = []
        self.after_response = lambda path, value: None
        real_client = httpx.Client

        def handle(request):
            kwargs = {}
            if request.content:
                kwargs["json"] = json.loads(request.content)
            if request.url.params:
                kwargs["params"] = {k: int(v) if k == "attempt" else v
                                    for k, v in request.url.params.items()}
            path = request.url.path
            self.http_calls.append(path)
            value = self.api.request(request.method, path, **kwargs)
            status = 200
            if isinstance(value, httpx.Response):
                status, value = value.status_code, value.json()
            result = httpx.Response(status, json=value)
            self.after_response(path, value)
            return result

        def client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return real_client(transport=httpx.MockTransport(handle), **kwargs)

        self.enterContext(patch("cli.acceptance_containment.httpx.Client", side_effect=client))
        self.owner("preflight", self.context())
        self.api.request("PUT", "/sources", json={
            "provider": "wwr", "tenant": "default", "enabled": True,
            "poll_interval": 604800, "concurrency": 1, "request_timeout": 10,
            "rate_limit": 1, "jitter_seconds": 0,
        })
        self.api.request("POST", "/sources/public-source/collect",
                         json={"profile_id": "profile", "request_id": "parent-request"})
        self.ctx = self.context(source_id="public-source", collection_operation_id="own-operation",
            collection_request_id="parent-request", collection_operation_attempt=1,
            **{**ChildContextInitializationTests.projection(), "child_discovery_requested": False})
        self.owner._capture_collection_identity(self.ctx)
        self.owner._last_stage = "poll"
        self.http_calls.clear()

    def snapshot(self, **kwargs):
        return self.owner._current_children(self.ctx, **kwargs)

    def test_later_parent_completion_requires_a_fresh_complete_child_snapshot(self):
        self.api.collection_state = self.api.operations[0]["state"] = "RUNNING"

        def advance(path, _):
            if path.endswith("/children") and not self.api.transitioned:
                self.api.advance()

        self.after_response = advance
        checked, parent, operations = self.snapshot()
        self.assertTrue(checked["complete"])
        self.assertEqual(parent["state"], "COMPLETED")
        self.assertEqual(checked["children"][0]["state"], operations[1]["state"])
        self.assertEqual(sum(p.endswith("/children") for p in self.http_calls), 2)
        self.assertEqual(self.http_calls[-1], "/operations")

    def test_child_progress_after_readback_reconciles_without_spending_extra_bootstrap_callbacks(self):
        self.owner._last_stage = "read"
        self.ctx["child_discovery_requested"] = True

        def advance(path, _):
            if path.endswith("/children") and not self.api.transitioned:
                self.api.advance()

        self.after_response = advance
        checked, _, operations = self.snapshot(require_complete=True)
        self.assertEqual(checked["children"][0]["state"], "BLOCKED")
        self.assertEqual(operations[1]["state"], "BLOCKED")
        self.assertEqual(sum(p.endswith("/children") for p in self.http_calls), 2)
        self.assertEqual(self.owner._child_bootstrap_callbacks_remaining, 3)
        self.assertFalse(self.owner._child_projection_observed)

    def test_queued_child_start_reconciles_its_current_attempt(self):
        self.api.operations[1].update(state="QUEUED", attempt_count=0)
        self.api.child.update(state="QUEUED", operation_attempt=0)

        def start(path, _):
            if path.endswith("/children"):
                self.api.operations[1].update(state="RUNNING", attempt_count=1)
                self.api.child.update(state="RUNNING", operation_attempt=1)

        self.after_response = start
        checked, _, _ = self.snapshot()
        self.assertEqual(checked["children"][0]["operation_attempt"], 1)
        self.assertEqual(sum(p.endswith("/children") for p in self.http_calls), 2)

    def test_active_child_retry_can_skip_intermediate_states_across_callbacks(self):
        self.snapshot()
        for state, attempt in (("RETRY_WAIT", 1), ("RUNNING", 2), ("BLOCKED", 3)):
            self.api.operations[1].update(state=state, attempt_count=attempt)
            self.api.child.update(state=state, operation_attempt=attempt)
            checked, parent, _ = self.snapshot()
            self.assertEqual((checked["children"][0]["state"], checked["children"][0]["operation_attempt"]),
                             (state, attempt))
            self.assertEqual(parent["attempt_count"], 1)
        self.api.operations[1]["attempt_count"] = self.api.child["operation_attempt"] = 4
        with self.assertRaisesRegex(ContainmentError, "CHILD_OPERATION_CHANGED"):
            self.snapshot()
        self.assertTrue(all(path == "/operations" or path.endswith("/children") for path in self.http_calls))

    def test_later_child_attempt_requires_matching_fresh_readback_without_initiating_retry(self):
        advanced = []

        def finish_retry(path, _):
            if path.endswith("/children") and not advanced:
                self.api.operations[1].update(state="BLOCKED", attempt_count=2)
                self.api.child.update(state="BLOCKED", operation_attempt=2)
                advanced.append(True)

        self.after_response = finish_retry
        checked, parent, operations = self.snapshot()
        self.assertEqual(checked["children"][0]["operation_attempt"], 2)
        self.assertEqual(operations[1]["attempt_count"], 2)
        self.assertEqual(parent["attempt_count"], 1)
        self.assertEqual(sum(path.endswith("/children") for path in self.http_calls), 2)
        self.assertTrue(all(path == "/operations" or path.endswith("/children") for path in self.http_calls))

    def test_two_children_share_one_total_budget_and_finish_with_exact_terminal_snapshots(self):
        second = deepcopy(self.api.child)
        second.update(request_id="request-2", company_id="company-2", company_source_id="aggregator-2",
                      operation_id="child-op-2", candidate_id="candidate-2", pending_sha256="d" * 64)
        operation = deepcopy(self.api.operations[1])
        operation.update(id="child-op-2", entity_id="company-2")
        operation["params"]["discovery_request_id"] = "request-2"
        self.api.operations.append(operation)
        original = self.api.children_response

        def children():
            value = original()
            value["children"].append(deepcopy(second))
            value["lineage_sha256"] = digest([{key: row[key] for key in CHILD_FIELDS if key not in CHILD_DYNAMIC}
                for row in value["children"]])
            value["sha256"] = digest({key: val for key, val in value.items() if key != "sha256"})
            return value

        self.api.children_response = children
        polls = []

        def progress(path, _):
            if not path.endswith("/children"):
                return
            polls.append(path)
            if len(polls) == 1:
                self.api.advance()
            elif len(polls) == 2:
                second.update(state="COMPLETED", operation_sha256="f" * 64)
                self.api.operations[2]["state"] = "COMPLETED"

        self.after_response = progress
        checked, _, operations = self.snapshot(require_complete=True)
        self.assertEqual(len(polls), 3)
        self.assertEqual(checked["operation_ids"], ["child-op-1", "child-op-2"])
        self.assertEqual([row["state"] for row in checked["children"]], ["BLOCKED", "COMPLETED"])
        self.assertEqual([row["state"] for row in operations[1:]], ["BLOCKED", "COMPLETED"])
        self.assertFalse(checked["acceptance_success"])

    def test_total_reconciliation_budget_is_bounded_and_never_grants_an_unsettled_snapshot(self):
        states = iter(("RETRY_WAIT", "QUEUED", "RUNNING"))

        def move(path, _):
            if path.endswith("/children"):
                state = next(states)
                attempt = 2 if state == "RUNNING" else 1
                self.api.operations[1].update(state=state, attempt_count=attempt)
                self.api.child.update(state=state, operation_attempt=attempt)

        self.after_response = move
        with self.assertRaisesRegex(ContainmentError, "CHILD_SNAPSHOT_NOT_STABLE"):
            self.snapshot()
        self.assertEqual(sum(p.endswith("/children") for p in self.http_calls), MAX_CHILD_SNAPSHOT_ATTEMPTS)
        self.assertEqual(len(self.http_calls), 1 + 2 * MAX_CHILD_SNAPSHOT_ATTEMPTS)
        self.assertIsNone(self.owner._child_last_checked)
        self.assertIsNone(self.owner._child_parent_binding)
        self.assertTrue(self.owner.claim_path.exists())

    def test_parent_attempt_profile_request_or_identity_drift_aborts_before_reconciliation(self):
        original = deepcopy(self.api.operations)
        mutations = (
            lambda p: p.update(id="foreign-parent"),
            lambda p: p.update(attempt_count=2),
            lambda p: p.update(attempt_count=True),
            lambda p: p.update(entity_id="foreign-profile"),
            lambda p: p.update(kind="COMPANY_DISCOVER"),
            lambda p: p["params"].update(profile_version="foreign-version"),
            lambda p: p["params"].update(source_id="foreign-source"),
            lambda p: p["params"].update(request_id="foreign-request"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.api.operations = deepcopy(original)
                self.http_calls.clear()
                self.after_response = lambda path, value: mutate(self.api.operations[0]) if path.endswith("/children") else None
                with self.assertRaises(ContainmentError):
                    self.snapshot()
                self.assertEqual(sum(p.endswith("/children") for p in self.http_calls), 1)
                self.assertIsNone(self.owner._child_last_checked)

    def test_child_identity_attempt_regression_or_terminal_rollback_is_not_retried(self):
        original = deepcopy(self.api.operations)
        mutations = (
            lambda p: p.update(id="foreign-operation"),
            lambda p: p.update(entity_id="foreign-company"),
            lambda p: p.update(attempt_count=0),
            lambda p: p.update(attempt_count=True),
            lambda p: p.update(attempt_count=-1),
            lambda p: p.update(state="UNKNOWN"),
            lambda p: p.update(state=[]),
            lambda p: p.update(state=True),
            lambda p: p["params"].update(discovery_request_id="foreign-request"),
            lambda p: p["params"].update(discovery_request_revision=True),
            lambda p: p["params"].update(profile_id="foreign-profile"),
            lambda p: p["params"].update(profile_version="foreign-version"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                self.api.operations = deepcopy(original)
                self.http_calls.clear()
                self.after_response = lambda path, value: mutate(self.api.operations[1]) if path.endswith("/children") else None
                with self.assertRaisesRegex(ContainmentError, "CHILD_OPERATION_CHANGED"):
                    self.snapshot()
                self.assertEqual(sum(p.endswith("/children") for p in self.http_calls), 1)
        self.api.operations = deepcopy(original)
        self.api.advance()
        self.after_response = lambda path, value: self.api.operations[1].update(state="RUNNING") if path.endswith("/children") else None
        with self.assertRaisesRegex(ContainmentError, "CHILD_OPERATION_CHANGED"):
            self.snapshot()

    def test_complete_family_cannot_gain_a_foreign_company_operation_between_snapshots(self):
        def foreign(path, _):
            if path.endswith("/children"):
                other = deepcopy(self.api.operations[1])
                other.update(id="foreign-operation", state="COMPLETED")
                self.api.operations.append(other)

        self.after_response = foreign
        with self.assertRaisesRegex(ContainmentError, "UNOWNED_COMPANY_DISCOVERY_EXISTS"):
            self.snapshot()
        self.assertEqual(sum(p.endswith("/children") for p in self.http_calls), 1)
        self.assertIsNone(self.owner._child_frozen)

    def test_fresh_foreign_parent_collection_is_still_reported_as_active(self):
        self.owner._child_context = self.ctx

        def foreign(path, _):
            if path.endswith("/children"):
                self.api.operations.append({"id": "foreign-collection", "kind": "DISCOVER",
                    "state": "RUNNING", "entity_id": "foreign-profile", "params": {"source_id": "foreign-source"}})

        self.after_response = foreign
        active = self.owner._active_discoveries_unfrozen()
        self.assertEqual([row["id"] for row in active], ["foreign-collection"])

    def test_full_operation_page_is_not_a_complete_inventory(self):
        self.api.operations.extend({"id": "old-" + str(i), "kind": "APPLY_MOCK", "state": "COMPLETED"}
                                   for i in range(98))
        with self.assertRaisesRegex(ContainmentError, "OPERATIONS_INVENTORY_INCOMPLETE"):
            self.snapshot()
        self.assertEqual(self.http_calls, ["/operations"])

    def test_bound_parent_and_complete_family_cannot_change_on_a_later_callback(self):
        self.snapshot()
        original = deepcopy(self.api.operations)
        self.api.operations[0]["attempt_count"] = 2
        self.ctx["collection_operation_attempt"] = None
        with self.assertRaisesRegex(ContainmentError, "CHILD_PARENT_IDENTITY_CHANGED"):
            self.snapshot()
        self.api.operations = deepcopy(original)
        self.api.child["request_id"] = "replacement-request"
        self.api.operations[1]["params"]["discovery_request_id"] = "replacement-request"
        with self.assertRaisesRegex(ContainmentError, "CHILD_CONTEXT_CHANGED"):
            self.snapshot()

    def test_incomplete_readback_never_authorizes_final_cleanup(self):
        self.api.collection_state = self.api.operations[0]["state"] = "RUNNING"
        with self.assertRaisesRegex(ContainmentError, "CHILD_READBACK_NOT_FINAL"):
            self.snapshot(require_complete=True)
        self.assertIsNone(self.owner._child_frozen)
        self.assertTrue(self.owner.claim_path.exists())

    def test_actual_same_owner_lease_is_renewed_between_bounded_http_reads(self):
        original = json.loads(self.owner.claim_path.read_text())
        self.after_response = lambda path, value: self._sleep(2)
        checked, _, _ = self.snapshot()
        renewed = json.loads(self.owner.claim_path.read_text())
        self.assertTrue(checked["complete"])
        for field in ("owner", "token", "project", "api_container_id", "scheduler_container_id"):
            self.assertEqual(renewed[field], original[field])
        self.assertGreater(renewed["lease_expires_at"], original["lease_expires_at"])
        self.assertGreater(self.owner._lease_expiry, self.now[0] + timedelta(seconds=15))

    def test_expired_entry_or_an_actual_in_read_gap_never_renews_or_continues(self):
        original = self.owner.claim_path.read_bytes()
        self._sleep(16)
        with self.assertRaisesRegex(ContainmentError, "CONTAINMENT_LEASE_EXPIRED"):
            self.snapshot()
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.owner.claim_path.read_bytes(), original)
        self.now[0] -= timedelta(seconds=16)
        self.after_response = lambda path, value: self._sleep(16)
        with self.assertRaisesRegex(ContainmentError, "CONTAINMENT_LEASE_EXPIRED"):
            self.snapshot()
        self.assertEqual(self.http_calls, ["/operations"])
        self.assertEqual(self.owner.claim_path.read_bytes(), original)

    def test_owner_token_change_during_read_is_not_reconciled_or_overwritten(self):
        def change(path, _):
            value = json.loads(self.owner.claim_path.read_text())
            value["token"] = "replacement-owner-token"
            self.owner.claim_path.write_text(json.dumps(value))

        self.after_response = change
        with self.assertRaisesRegex(ContainmentError, "CONTAINMENT_OWNERSHIP_CHANGED"):
            self.snapshot()
        self.assertEqual(self.http_calls, ["/operations"])
        self.assertEqual(json.loads(self.owner.claim_path.read_text())["token"], "replacement-owner-token")
        self.assertIsNone(self.owner._child_last_checked)


if __name__ == "__main__":
    unittest.main()
