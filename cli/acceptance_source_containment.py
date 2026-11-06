"""Containment for a public collection and its exact native-company children.

This class is a drop-in extension for ``OwnedPublicContainment``.  It admits
only child operations returned by the database-backed parent/attempt read
model.  The local absent-only claim is cooperative operator ownership, not an
API CAS: source fingerprints detect drift but cannot prevent an uncooperative
writer between a read and a write.  Callers must exclude other source-policy
writers for this isolated fixture project.

At the existing finalization/``cleanup`` callback this class freezes the complete child
lineage, then disables only newly created company-source rows proven by that
lineage.  It disables a global collector row only when the preflight inventory
proved that exact collector ID was absent.  Rows and jobs are retained. Any
uncertain readback leaves the parent lease and scheduler pause in place.
"""

import hashlib
import json
import re
from urllib.parse import quote

import httpx

from cli.acceptance_containment import (
    API_TIMEOUT_SECONDS,
    ACTIVE_COLLECTION_STATES,
    ContainmentError,
    OwnedPublicContainment,
    _require,
)
from cli.acceptance_public_source import FINGERPRINT_FIELDS

CHILD_FIELDS = (
    "request_id", "revision", "company_id", "company_source_id", "profile_id",
    "profile_version", "source_id", "parent_operation_id", "parent_operation_attempt",
    "trigger_job_id", "candidate_id", "posting_id", "snapshot_id", "snapshot_sha256",
    "quality_observation_id", "operation_id", "state", "authorization", "reason",
    "operation_attempt", "operation_sha256", "pending_sha256",
)
CHILD_DYNAMIC = {"state", "authorization", "reason", "operation_attempt", "operation_sha256"}
CHILD_TERMINAL = {
    "COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "DEAD_LETTERED", "DISCARDED",
    "NEEDS_ORIGIN_REVIEW", "DISMISSED",
}
ENROLLED_SOURCE_FIELDS = {
    "source_id", "company_source_id", "operation_id", "request_id", "policy",
    "policy_sha256", "audit_id", "audit_sha256", "pending_sha256",
    "receipt_sha256", "envelope_sha256",
}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
MAX_INVENTORY_ROWS = 10000


def _canonical_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                    allow_nan=False).encode("utf-8")
    ).hexdigest()


class OwnedPublicSourceContainment(OwnedPublicContainment):
    """Drop-in public-source containment with exact child-source cleanup."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._child_context = None
        self._child_frozen = None
        self._initial_all_sources = None
        self._initial_company_sources = None
        self._initial_company_operation_ids = None
        self._child_cleanup_rows = {}
        self._child_cleanup_record_path = self.run_dir / "child-source-cleanup.json"

    @staticmethod
    def _children_terminal(children):
        return all(
            isinstance(row, dict)
            and row.get("state") in CHILD_TERMINAL
            and (row.get("operation_id") is not None or row.get("state") in {"NEEDS_ORIGIN_REVIEW", "DISMISSED"})
            for row in children
        )

    def _validate_children_response(
        self,
        response,
        *,
        profile_id,
        profile_version,
        source_id,
        parent_id,
        parent_attempt,
        parent_request_id,
        require_complete,
        expected_parent_state=None,
    ):
        """Validate response digest, exact parent joins, bounded rows and enrollments."""
        _require(isinstance(response, dict), "CHILD_READBACK_INVALID")
        _require(set(response) == {
            "schema_version", "profile_id", "profile_version", "source_id",
            "parent_operation_id", "parent_operation_attempt", "parent_request_id",
            "complete", "limit", "children", "enrolled_sources", "lineage_sha256", "sha256",
        }, "CHILD_READBACK_SCHEMA_INVALID")
        _require(response.get("schema_version") == 1 and type(response.get("schema_version")) is int,
                 "CHILD_READBACK_SCHEMA_INVALID")
        for key, expected in (
            ("profile_id", profile_id), ("profile_version", profile_version),
            ("source_id", source_id), ("parent_operation_id", parent_id),
            ("parent_operation_attempt", parent_attempt), ("parent_request_id", parent_request_id),
        ):
            _require(type(response.get(key)) is type(expected) and response.get(key) == expected,
                     "CHILD_IDENTITY_CHANGED")
        _require(type(response.get("limit")) is int and response["limit"] == 2,
                 "CHILD_READBACK_LIMIT_INVALID")
        children = response.get("children")
        enrolled = response.get("enrolled_sources")
        _require(isinstance(children, list) and len(children) <= 2,
                 "CHILD_READBACK_INCOMPLETE")
        _require(isinstance(enrolled, list) and len(enrolled) <= 2,
                 "CHILD_ENROLLMENT_INVALID")
        complete = response.get("complete")
        _require(type(complete) is bool, "CHILD_READBACK_INCOMPLETE")
        if require_complete:
            _require(complete is True and expected_parent_state == "COMPLETED",
                     "CHILD_READBACK_NOT_FINAL")
        elif expected_parent_state == "RUNNING":
            _require(complete is False, "CHILD_READBACK_PARENT_STATE_INVALID")
        elif expected_parent_state == "COMPLETED":
            _require(complete is True, "CHILD_READBACK_PARENT_STATE_INVALID")
        elif expected_parent_state is not None:
            raise ContainmentError("CHILD_PARENT_STATE_INVALID")

        seen_requests, seen_operations, stable_rows = set(), set(), []
        for row in children:
            _require(isinstance(row, dict) and set(row) == set(CHILD_FIELDS),
                     "CHILD_RECORD_INVALID")
            for key, expected in (
                ("profile_id", profile_id), ("profile_version", profile_version),
                ("source_id", source_id), ("parent_operation_id", parent_id),
                ("parent_operation_attempt", parent_attempt),
            ):
                _require(type(row.get(key)) is type(expected) and row.get(key) == expected,
                         "CHILD_IDENTITY_CHANGED")
            for key in ("request_id", "company_id", "company_source_id", "trigger_job_id",
                        "candidate_id", "posting_id", "snapshot_id", "quality_observation_id"):
                _require(isinstance(row.get(key), str) and SAFE_ID.fullmatch(row[key]) is not None,
                         "CHILD_RECORD_INVALID")
            _require(type(row.get("revision")) is int and row["revision"] >= 1
                     and type(row.get("operation_attempt")) is int and row["operation_attempt"] >= 0,
                     "CHILD_RECORD_INVALID")
            _require(row.get("state") in CHILD_TERMINAL | {"QUEUED", "RUNNING", "RETRY_WAIT"},
                     "CHILD_RECORD_INVALID")
            for key in ("snapshot_sha256", "pending_sha256"):
                _require(isinstance(row.get(key), str) and SHA256.fullmatch(row[key]) is not None,
                         "CHILD_RECORD_INVALID")
            operation_id = row.get("operation_id")
            operation_sha = row.get("operation_sha256")
            if operation_id is None:
                _require(row["state"] in {"NEEDS_ORIGIN_REVIEW", "DISMISSED"}
                         and operation_sha is None, "CHILD_RECORD_INVALID")
            else:
                _require(isinstance(operation_id, str) and SAFE_ID.fullmatch(operation_id) is not None
                         and isinstance(operation_sha, str) and SHA256.fullmatch(operation_sha) is not None,
                         "CHILD_RECORD_INVALID")
                _require(operation_id not in seen_operations, "CHILD_RECORD_DUPLICATE")
                seen_operations.add(operation_id)
            for key in ("authorization", "reason"):
                value = row.get(key)
                _require(value is None or (isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value)),
                         "CHILD_RECORD_INVALID")
            _require(row["request_id"] not in seen_requests, "CHILD_RECORD_DUPLICATE")
            seen_requests.add(row["request_id"])
            stable_rows.append({key: row[key] for key in CHILD_FIELDS if key not in CHILD_DYNAMIC})

        ordered = sorted(children, key=lambda row: row["request_id"])
        stable_rows = [{key: row[key] for key in CHILD_FIELDS if key not in CHILD_DYNAMIC} for row in ordered]
        _require(response.get("lineage_sha256") == _canonical_digest(stable_rows),
                 "CHILD_LINEAGE_DIGEST_INVALID")
        _require(response.get("sha256") == _canonical_digest({key: value for key, value in response.items() if key != "sha256"}),
                 "CHILD_READBACK_DIGEST_INVALID")

        rows_by_identity = {
            (row["request_id"], row["operation_id"], row["pending_sha256"]): row
            for row in ordered
        }
        seen_source_ids, seen_company_ids = set(), set()
        for item in enrolled:
            _require(isinstance(item, dict) and set(item) == ENROLLED_SOURCE_FIELDS,
                     "CHILD_ENROLLMENT_INVALID")
            for key in ("source_id", "company_source_id", "operation_id", "request_id", "audit_id"):
                _require(isinstance(item.get(key), str) and SAFE_ID.fullmatch(item[key]) is not None,
                         "CHILD_ENROLLMENT_INVALID")
            for key in ("policy_sha256", "audit_sha256", "pending_sha256", "receipt_sha256", "envelope_sha256"):
                _require(isinstance(item.get(key), str) and SHA256.fullmatch(item[key]) is not None,
                         "CHILD_ENROLLMENT_INVALID")
            policy = item.get("policy")
            _require(isinstance(policy, dict) and all(key in policy for key in FINGERPRINT_FIELDS)
                     and policy.get("id") == item["source_id"]
                     and policy.get("provider") and policy.get("tenant")
                     and policy.get("enabled") is True,
                     "CHILD_ENROLLMENT_POLICY_INVALID")
            _require(item["policy_sha256"] == _canonical_digest({key: policy[key] for key in FINGERPRINT_FIELDS}),
                     "CHILD_ENROLLMENT_POLICY_INVALID")
            row = rows_by_identity.get((item["request_id"], item["operation_id"], item["pending_sha256"]))
            # The child row names the originating aggregator company source;
            # an enrollment projection names the newly enrolled native ATS
            # company source. Their binding is the shared request/op/pending
            # proof plus company_id on current company-source readback.
            _require(row is not None, "CHILD_ENROLLMENT_UNBOUND")
            _require(item["source_id"] != source_id and item["source_id"] not in seen_source_ids
                     and item["company_source_id"] not in seen_company_ids,
                     "CHILD_ENROLLMENT_DUPLICATE")
            seen_source_ids.add(item["source_id"])
            seen_company_ids.add(item["company_source_id"])
        return {
            "children": ordered,
            "enrolled_sources": sorted(enrolled, key=lambda item: (item["source_id"], item["company_source_id"])),
            "operation_ids": sorted(row["operation_id"] for row in ordered if row["operation_id"]),
            "request_ids": sorted(row["request_id"] for row in ordered),
            "lineage_sha256": response["lineage_sha256"],
            "complete": complete,
            "acceptance_success": bool(complete and all(row["state"] == "COMPLETED" for row in ordered)),
        }

    @staticmethod
    def _rows(response, key, error_code):
        _require(isinstance(response, dict) and isinstance(response.get(key), list), error_code)
        rows = response[key]
        _require(len(rows) < MAX_INVENTORY_ROWS and all(isinstance(row, dict) for row in rows), error_code)
        return rows

    def _operations(self):
        return self._rows(self._api_get("/operations"), "operations", "OPERATIONS_RESPONSE_INVALID")

    def _parent_operation(self, operations):
        parent_id = self._own_operation_id
        if not parent_id:
            return None
        matches = [row for row in operations if row.get("id") == parent_id]
        _require(len(matches) == 1, "CHILD_PARENT_OPERATION_MISSING")
        parent = matches[0]
        params = parent.get("params")
        _require(parent.get("kind") == "DISCOVER" and parent.get("entity_id") == self._own_profile_id
                 and isinstance(params, dict) and params.get("source_id") == self._own_source_id
                 and params.get("request_id") == self._own_request_id,
                 "CHILD_PARENT_OPERATION_CHANGED")
        return parent

    def _current_children(self, context, *, require_complete=False):
        operations = self._operations()
        parent = self._parent_operation(operations)
        _require(parent is not None, "CHILD_PARENT_OPERATION_MISSING")
        state = parent.get("state")
        _require(state in {"RUNNING", "COMPLETED"}, "CHILD_PARENT_NOT_AUTHORIZED")
        attempt = context.get("collection_operation_attempt")
        if attempt is None:
            attempt = parent.get("attempt_count")
        _require(type(attempt) is int and attempt >= 1 and parent.get("attempt_count") == attempt,
                 "CHILD_PARENT_ATTEMPT_CHANGED")
        params = parent["params"]
        profile_version = params.get("profile_version")
        _require(isinstance(profile_version, str) and profile_version,
                 "CHILD_PARENT_PROFILE_VERSION_INVALID")
        profile_id = self._own_profile_id
        path = "/profiles/" + quote(profile_id, safe="") + "/sources/" + quote(self._own_source_id, safe="")
        path += "/collections/" + quote(self._own_operation_id, safe="") + "/children"
        raw = self.request("GET", path, params={"attempt": attempt})
        checked = self._validate_children_response(
            raw,
            profile_id=profile_id,
            profile_version=profile_version,
            source_id=self._own_source_id,
            parent_id=self._own_operation_id,
            parent_attempt=attempt,
            parent_request_id=self._own_request_id,
            require_complete=require_complete,
            expected_parent_state=state,
        )
        self._validate_child_context(context, checked)
        return checked, parent, operations

    def _validate_child_context(self, context, checked):
        requested = context.get("child_discovery_requested")
        _require(type(requested) is bool, "CHILD_CONTEXT_INVALID")
        operation_ids = context.get("owned_child_operation_ids")
        request_ids = context.get("owned_child_request_ids")
        lineage = context.get("owned_child_lineage_sha256")
        enrolled = context.get("owned_child_sources")
        _require(isinstance(operation_ids, list) and isinstance(request_ids, list)
                 and isinstance(enrolled, list), "CHILD_CONTEXT_INVALID")
        if requested:
            _require(operation_ids == checked["operation_ids"] and request_ids == checked["request_ids"],
                     "CHILD_CONTEXT_CHANGED")
            if lineage is not None:
                _require(lineage == checked["lineage_sha256"], "CHILD_CONTEXT_CHANGED")
            if checked["complete"]:
                _require(lineage == checked["lineage_sha256"] and enrolled == checked["enrolled_sources"],
                         "CHILD_CONTEXT_CHANGED")
        else:
            _require(not operation_ids and not request_ids and lineage is None and not enrolled,
                     "CHILD_CONTEXT_CHANGED")

    def _active_discoveries_unfrozen(self):
        active_public = super()._active_discoveries()
        operations = self._operations()
        if self._initial_company_operation_ids is None:
            self._initial_company_operation_ids = {
                row.get("id") for row in operations
                if row.get("kind") == "COMPANY_DISCOVER" and isinstance(row.get("id"), str)
            }
        parent = self._parent_operation(operations)
        active_children = [
            row for row in operations
            if row.get("kind") == "COMPANY_DISCOVER" and row.get("state") in ACTIVE_COLLECTION_STATES
        ]
        if not active_children:
            known_child_ids = set()
            if parent is not None and parent.get("state") in {"RUNNING", "COMPLETED"}:
                known_child_ids = self._current_children(self._child_context or {},
                    require_complete=parent.get("state") == "COMPLETED")[0]["operation_ids"]
            self._reject_unowned_company_operations(operations, known_child_ids)
            return active_public
        if self._child_frozen is not None:
            checked = self._child_frozen
        elif parent is not None and parent.get("state") in {"RUNNING", "COMPLETED"} and self._child_context is not None:
            checked, _, _ = self._current_children(self._child_context, require_complete=parent.get("state") == "COMPLETED")
        else:
            raise ContainmentError("UNOWNED_COMPANY_DISCOVERY_EXISTS")
        children = {row["operation_id"]: row for row in checked["children"] if row["operation_id"]}
        self._reject_unowned_company_operations(operations, set(children))
        for operation in active_children:
            child = children.get(operation.get("id"))
            _require(child is not None and operation.get("entity_id") == child["company_id"],
                     "UNOWNED_COMPANY_DISCOVERY_EXISTS")
            params = operation.get("params")
            _require(isinstance(params, dict)
                     and params.get("discovery_request_id") == child["request_id"]
                     and params.get("discovery_request_revision") == child["revision"]
                     and params.get("profile_id") == child["profile_id"]
                     and params.get("profile_version") == child["profile_version"],
                     "CHILD_OPERATION_CHANGED")
            _require(operation.get("state") == child["state"], "CHILD_OPERATION_CHANGED")
        return active_public

    def _reject_unowned_company_operations(self, operations, owned_ids):
        baseline = self._initial_company_operation_ids or set()
        for row in operations:
            if row.get("kind") != "COMPANY_DISCOVER":
                continue
            if row.get("id") not in baseline and row.get("id") not in owned_ids:
                raise ContainmentError("UNOWNED_COMPANY_DISCOVERY_EXISTS")

    def _capture_initial_child_resources(self):
        source_rows = self._rows(self._api_get("/sources"), "sources", "SOURCE_RESPONSE_INVALID")
        company_rows = self._rows(self._api_get("/company-sources"), "company_sources", "COMPANY_SOURCE_RESPONSE_INVALID")
        source_map = {}
        for row in source_rows:
            _require(isinstance(row.get("id"), str) and row["id"] not in source_map,
                     "SOURCE_INVENTORY_AMBIGUOUS")
            _require(all(key in row for key in FINGERPRINT_FIELDS), "SOURCE_INVENTORY_INCOMPLETE")
            source_map[row["id"]] = dict(row)
        company_map = {}
        for row in company_rows:
            _require(isinstance(row.get("id"), str) and row["id"] not in company_map,
                     "COMPANY_SOURCE_INVENTORY_AMBIGUOUS")
            company_map[row["id"]] = dict(row)
        self._initial_all_sources = source_map
        self._initial_company_sources = company_map

    def _admit(self, context):
        super()._admit(context)
        self._capture_initial_child_resources()

    def _read_current_sources(self):
        rows = self._rows(self._api_get("/sources"), "sources", "SOURCE_RESPONSE_INVALID")
        result = {}
        for row in rows:
            _require(isinstance(row.get("id"), str) and row["id"] not in result
                     and all(key in row for key in FINGERPRINT_FIELDS), "SOURCE_INVENTORY_AMBIGUOUS")
            result[row["id"]] = row
        return result
    def _read_current_company_sources(self):
        rows = self._rows(self._api_get("/company-sources"), "company_sources", "COMPANY_SOURCE_RESPONSE_INVALID")
        result = {}
        for row in rows:
            _require(isinstance(row.get("id"), str) and row["id"] not in result,
                     "COMPANY_SOURCE_INVENTORY_AMBIGUOUS")
            result[row["id"]] = row
        return result

    def _policy_hash(self, row):
        _require(isinstance(row, dict) and all(key in row for key in FINGERPRINT_FIELDS),
                 "SOURCE_POLICY_INVALID")
        return _canonical_digest({key: row[key] for key in FINGERPRINT_FIELDS})

    def _validate_child_resources(self, checked, *, after_cleanup=False):
        sources = self._read_current_sources()
        company_sources = self._read_current_company_sources()
        enrolled_by_source = {item["source_id"]: item for item in checked["enrolled_sources"]}
        for source_id, item in enrolled_by_source.items():
            initial = self._initial_all_sources.get(source_id)
            current = sources.get(source_id)
            _require(current is not None, "CHILD_SOURCE_POLICY_CHANGED")
            expected_source_hash = item["policy_sha256"]
            cleanup_row = self._child_cleanup_rows.get(source_id, {})
            if after_cleanup and initial is None and cleanup_row.get("state") == "GLOBAL_DISABLED":
                expected_source = dict(item["policy"])
                expected_source["enabled"] = False
                expected_source_hash = self._policy_hash(expected_source)
            _require(self._policy_hash(current) == expected_source_hash,
                     "CHILD_SOURCE_POLICY_CHANGED")
            company = company_sources.get(item["company_source_id"])
            _require(company is not None, "CHILD_COMPANY_SOURCE_MISSING")
            _require(company.get("company_id") in {row["company_id"] for row in checked["children"]}
                     and company.get("collector_source_id") == source_id
                     and company.get("provider") == item["policy"].get("provider")
                     and company.get("tenant") == item["policy"].get("tenant"),
                     "CHILD_COMPANY_SOURCE_CHANGED")
            if initial is not None:
                _require(self._policy_hash(initial) == item["policy_sha256"],
                         "PREEXISTING_CHILD_COLLECTOR_CHANGED")
            company_initial = self._initial_company_sources.get(item["company_source_id"])
            if company_initial is not None:
                _require(company_initial == company,
                         "PREEXISTING_COMPANY_SOURCE_CHANGED")
            if after_cleanup:
                expected_enabled = False if self._child_cleanup_rows.get(item["company_source_id"], {}).get("state") == "DISABLED" else company_initial.get("enabled") if company_initial else True
                _require(company.get("enabled") is expected_enabled, "CHILD_COMPANY_SOURCE_CLEANUP_UNCONFIRMED")
        return sources, company_sources

    @staticmethod
    def _company_source_identity(row):
        return {key: value for key, value in row.items() if key != "enabled"}

    def _write_child_cleanup_receipt(self):
        record = {
            "schema_version": 1,
            "project": self.project_name,
            "source_revision": self.source_revision,
            "source_tree": self.source_tree,
            "parent_operation_id": self._own_operation_id,
            "parent_request_id": self._own_request_id,
            "lineage_sha256": None if self._child_frozen is None else self._child_frozen["lineage_sha256"],
            "acceptance_success": False if self._child_frozen is None else self._child_frozen["acceptance_success"],
            "owned_child_sources": [
                {key: value for key, value in row.items() if key not in {"policy"}}
                for row in (() if self._child_frozen is None else self._child_frozen["enrolled_sources"])
            ],
            "cleanup": self._child_cleanup_rows,
        }
        self._atomic_runtime_json(self._child_cleanup_record_path, record)

    def _atomic_runtime_json(self, path, value):
        """Durably update a private ignored run-dir receipt without following links."""
        from pathlib import Path
        import os
        import tempfile

        _require(path.parent == Path(self.run_dir) and path.parent.is_dir(), "CHILD_RECEIPT_PATH_INVALID")
        _require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(),
                 "CHILD_RECEIPT_PATH_INVALID")
        descriptor, temp_name = tempfile.mkstemp(prefix=".child-cleanup-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(),
                     "CHILD_RECEIPT_PATH_INVALID")
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _freeze_child_lineage(self, context):
        checked, parent, _ = self._current_children(context, require_complete=True)
        _require(parent.get("state") == "COMPLETED", "CHILD_PARENT_NOT_TERMINAL")
        _require(self._children_terminal(checked["children"]), "CHILD_OPERATION_ACTIVE")
        _require(self._child_frozen is None or (
            self._child_frozen["lineage_sha256"] == checked["lineage_sha256"]
            and self._child_frozen["enrolled_sources"] == checked["enrolled_sources"]
        ), "CHILD_LINEAGE_CHANGED_AFTER_FREEZE")
        self._child_frozen = checked
        self._child_context = dict(context)
        self._validate_child_resources(checked)
        self._write_child_cleanup_receipt()
        return checked

    def _assert_frozen_operations(self):
        _require(self._child_frozen is not None, "CHILD_LINEAGE_NOT_FROZEN")
        operations = self._operations()
        parent = self._parent_operation(operations)
        _require(parent is not None and parent.get("state") == "COMPLETED",
                 "CHILD_PARENT_OPERATION_CHANGED")
        child_rows = {row["operation_id"]: row for row in self._child_frozen["children"] if row["operation_id"]}
        self._reject_unowned_company_operations(operations, set(child_rows))
        linked_resources = {
            value
            for item in self._child_frozen["enrolled_sources"]
            for value in (item["source_id"], item["company_source_id"])
        }
        for operation in operations:
            if operation.get("state") not in ACTIVE_COLLECTION_STATES:
                continue
            params = operation.get("params") if isinstance(operation.get("params"), dict) else {}
            associated = {
                operation.get("source_id"), operation.get("entity_id"),
                params.get("source_id"), params.get("company_source_id"),
            } & linked_resources
            if associated and operation.get("id") not in child_rows:
                raise ContainmentError("CHILD_SOURCE_OPERATION_EXISTS")
        for operation in operations:
            if operation.get("kind") == "COMPANY_DISCOVER":
                if operation.get("id") in child_rows:
                    child = child_rows[operation["id"]]
                    _require(operation.get("entity_id") == child["company_id"]
                             and operation.get("state") == child["state"],
                             "CHILD_OPERATION_CHANGED")
                    params = operation.get("params")
                    _require(isinstance(params, dict)
                             and params.get("discovery_request_id") == child["request_id"]
                             and params.get("discovery_request_revision") == child["revision"]
                             and params.get("profile_id") == child["profile_id"]
                             and params.get("profile_version") == child["profile_version"],
                             "CHILD_OPERATION_CHANGED")
                elif operation.get("state") in ACTIVE_COLLECTION_STATES:
                    raise ContainmentError("UNOWNED_COMPANY_DISCOVERY_EXISTS")
        for operation_id, child in child_rows.items():
            matches = [row for row in operations if row.get("id") == operation_id]
            _require(len(matches) == 1 and matches[0].get("kind") == "COMPANY_DISCOVER"
                     and matches[0].get("entity_id") == child["company_id"]
                     and matches[0].get("state") == child["state"],
                     "CHILD_OPERATION_CHANGED")
        _require(self._children_terminal(self._child_frozen["children"]), "CHILD_OPERATION_ACTIVE")
        _require(not super()._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")

    def _send_owned_write(self, method, path, body):
        self._validate_api_path(path)
        self._verify_targets()
        self._ensure_lease()
        self._verify_api()
        self._assert_frozen_operations()
        try:
            if self._injected_request is not None:
                result = self._injected_request(method, path, json=body)
            else:
                with httpx.Client(base_url=self.base_url, timeout=API_TIMEOUT_SECONDS,
                                  trust_env=False, follow_redirects=False) as client:
                    response = client.request(method, path, json=body)
                    response.raise_for_status()
                    result = response.json()
        except Exception as error:
            raise ContainmentError("CHILD_SOURCE_CLEANUP_WRITE_FAILED") from error
        self._verify_targets()
        self._ensure_lease()
        self._verify_api()
        return result

    def _disable_child_company_source(self, item, company_before):
        source_id = item["company_source_id"]
        _require(source_id not in self._initial_company_sources,
                 "PREEXISTING_COMPANY_SOURCE_NOT_OWNED")
        _require(company_before.get("enabled") is True
                 and company_before.get("collector_source_id") == item["source_id"],
                 "CHILD_COMPANY_SOURCE_CLEANUP_SCOPE_INVALID")
        self._child_cleanup_rows[source_id] = {
            "company_source_id": source_id,
            "source_id": item["source_id"],
            "state": "DISABLE_INTENT",
            "before_identity": self._company_source_identity(company_before),
            "enabled_before": True,
        }
        self._write_child_cleanup_receipt()
        self._send_owned_write("PUT", "/company-sources/" + quote(source_id, safe="") + "/enabled", {"enabled": False})
        current = self._read_current_company_sources().get(source_id)
        _require(current is not None and self._company_source_identity(current) == self._company_source_identity(company_before)
                 and current.get("enabled") is False,
                 "CHILD_COMPANY_SOURCE_CLEANUP_UNCONFIRMED")
        self._child_cleanup_rows[source_id].update(state="DISABLED", enabled_after=False)
        self._write_child_cleanup_receipt()

    def _disable_new_global_source(self, item, source_before):
        source_id = item["source_id"]
        prior = self._child_cleanup_rows.get(source_id)
        if source_id in self._initial_all_sources:
            _require(self._policy_hash(source_before) == item["policy_sha256"],
                     "PREEXISTING_CHILD_COLLECTOR_CHANGED")
            return
        if prior is not None and prior.get("state") == "GLOBAL_DISABLED":
            expected = dict(item["policy"])
            expected["enabled"] = False
            _require(self._policy_hash(source_before) == self._policy_hash(expected),
                     "CHILD_SOURCE_CLEANUP_UNCONFIRMED")
            return
        _require(prior is None, "CHILD_SOURCE_CLEANUP_INCOMPLETE")
        _require(self._policy_hash(source_before) == item["policy_sha256"]
                 and source_before.get("enabled") is True,
                 "CHILD_SOURCE_CLEANUP_SCOPE_INVALID")
        _require(item["company_source_id"] not in self._initial_company_sources,
                 "PREEXISTING_COMPANY_SOURCE_NOT_OWNED")
        self._child_cleanup_rows.setdefault(source_id, {
            "source_id": source_id,
            "state": "GLOBAL_DISABLE_INTENT",
            "policy_sha256_before": item["policy_sha256"],
            "enabled_before": True,
        })
        self._write_child_cleanup_receipt()
        body = {key: source_before[key] for key in (
            "provider", "tenant", "enabled", "poll_interval", "concurrency",
            "request_timeout", "rate_limit", "jitter_seconds",
        )}
        body["enabled"] = False
        self._send_owned_write("PUT", "/sources", body)
        current = self._read_current_sources().get(source_id)
        expected = dict(source_before)
        expected["enabled"] = False
        _require(current is not None and self._policy_hash(current) == self._policy_hash(expected),
                 "CHILD_SOURCE_CLEANUP_UNCONFIRMED")
        self._child_cleanup_rows[source_id].update(state="GLOBAL_DISABLED", enabled_after=False)
        self._write_child_cleanup_receipt()

    def cleanup_owned_child_sources(self, context):
        """Freeze lineage and disable only proven new rows; retain all records."""
        checked = self._child_frozen or self._freeze_child_lineage(context)
        if self._child_frozen is not None:
            self._validate_child_context(context, checked)
        _require(checked["complete"] and self._children_terminal(checked["children"]),
                 "CHILD_OPERATION_ACTIVE")
        self._assert_frozen_operations()
        if self._child_cleanup_rows:
            _require(all(row.get("state") in {"DISABLED", "GLOBAL_DISABLED"}
                         for row in self._child_cleanup_rows.values()),
                     "CHILD_SOURCE_CLEANUP_INCOMPLETE")
        sources, company_sources = self._validate_child_resources(
            checked, after_cleanup=bool(self._child_cleanup_rows)
        )
        for item in checked["enrolled_sources"]:
            company_id = item["company_source_id"]
            company = company_sources.get(company_id)
            _require(company is not None, "CHILD_COMPANY_SOURCE_MISSING")
            if company_id not in self._initial_company_sources:
                row = self._child_cleanup_rows.get(company_id)
                if row is None or row.get("state") != "DISABLED":
                    self._disable_child_company_source(item, company)
            source = sources.get(item["source_id"])
            _require(source is not None, "CHILD_SOURCE_MISSING")
            self._disable_new_global_source(item, source)
            # Refresh both inventories after every linked source transition.
            sources, company_sources = self._validate_child_resources(checked, after_cleanup=True)
        self._assert_frozen_operations()
        self._write_child_cleanup_receipt()
        return {
            "lineage_sha256": checked["lineage_sha256"],
            "acceptance_success": checked["acceptance_success"],
            "company_sources_disabled": sorted(
                key for key, value in self._child_cleanup_rows.items()
                if value.get("state") == "DISABLED"
            ),
            "global_sources_disabled": sorted(
                key for key, value in self._child_cleanup_rows.items()
                if value.get("state") == "GLOBAL_DISABLED"
            ),
        }

    def _active_discoveries(self):
        return self._active_discoveries_unfrozen() if self._child_frozen is None else self._active_discoveries_frozen()

    def _active_discoveries_frozen(self):
        self._assert_frozen_operations()
        return super()._active_discoveries()

    def __call__(self, stage, context):
        self._child_context = dict(context) if isinstance(context, dict) else None
        if self._child_frozen is not None:
            self._validate_child_context(context, self._child_frozen)
        if stage == "release":
            # Prove child cleanup before the parent releases its scheduler or
            # deletes its lease. A failed readback therefore fails closed.
            self.cleanup_owned_child_sources(context)
            self._assert_frozen_operations()
            self._validate_child_resources(self._child_frozen, after_cleanup=True)
        result = super().__call__(stage, context)
        if stage in {"finalize", "cleanup"}:
            result["child_source_cleanup"] = self.cleanup_owned_child_sources(context)
        elif stage == "release" and self._child_frozen is not None:
            self._assert_frozen_operations()
            self._validate_child_resources(self._child_frozen, after_cleanup=True)
            result["child_lineage_sha256"] = self._child_frozen["lineage_sha256"]
            result["child_acceptance_success"] = self._child_frozen["acceptance_success"]
        return result
