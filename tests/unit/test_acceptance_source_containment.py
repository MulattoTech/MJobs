"""Focused, offline validation for parent-bound source containment."""

import hashlib
import json
import unittest
from copy import deepcopy

from cli.acceptance_source_containment import (
    CHILD_DYNAMIC,
    CHILD_FIELDS,
    ContainmentError,
    OwnedPublicSourceContainment,
)
from cli.acceptance_public_source import FINGERPRINT_FIELDS


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


if __name__ == "__main__":
    unittest.main()
