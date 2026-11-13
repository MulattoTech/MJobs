"""Focused, offline validation for parent-bound source containment."""

import hashlib
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx

from cli.acceptance_source_containment import (
    CHILD_DYNAMIC,
    CHILD_FIELDS,
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


if __name__ == "__main__":
    unittest.main()
