"""Fail-closed structural checks for the MJ-REQ-292 durable scoreboard."""
from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from tools.verification import acceptance_ledger as ledger

ROOT = Path(__file__).resolve().parents[2]


def source_requirements(root: Path = ROOT) -> list[str]:
    source = (root / "docs/spec/source/MJobs_Startup_Prompt.original.md").read_text(
        encoding="utf-8-sig"
    )
    section = source.split("# 292. END-TO-END ACCEPTANCE TEST\n", 1)[1]
    section = section.split("\n# 293.", 1)[0]
    requirements = []
    for line in section.splitlines():
        if line and line[0].isdigit() and ". " in line:
            number, requirement = line.split(". ", 1)
            if number.isdecimal():
                requirements.append(requirement)
    return requirements


def make_ledger(root: Path = ROOT) -> dict:
    requirements = source_requirements(root)
    steps = []
    for number, original in enumerate(requirements, start=1):
        steps.append({
            "number": number,
            "phase": "A",
            "original_requirement": original,
            "expected_observable_result": f"Observable result for step {number}",
            "implementation_references": [],
            "status": "NOT_RUN",
            "evidence_of_existing_behavior": ([{
                "artifact": "docs/evidence/2026-10-11-durable-onboarding.json",
                "classification": "HISTORICAL_SCOPED_NOT_CURRENT_ACCEPTANCE",
            }] if number == 1 else []),
            "missing_behavior": "",
            "responsible_agent": "",
            "exclusive_source_ownership": [],
            "ownership_status": "EXCLUSIVE_ISOLATED_IMPLEMENTATION",
            "required_automated_test": "",
            "required_integration_validation": "",
            "verification": {
                "tested_commit": None,
                "evidence_artifact": None,
                "evidence_sha256": None,
                "command": None,
                "observed_result": None,
                "executed_at_utc": None,
                "run_id": None,
                "evidence_kind": None,
                "continuity": {},
            },
            "review": {"status": "NOT_RUN", "evidence_artifact": None},
            "merge": {"status": "NOT_MERGED", "pr_url": None,
                      "integrated_commit": None},
        })
    return {
        "schema_version": 1,
        "requirement_id": "MJ-REQ-292",
        "issue_url": "https://github.com/MulattoTech/MJobs/issues/293",
        "acceptance_policy": "PASS rows are claims; this tool checks structure only.",
        "specification": {
            "working": "docs/spec/areas/delivery_control.md",
            "original": "docs/spec/source/MJobs_Startup_Prompt.original.md",
            "original_lines": [7659, 7725],
        },
        "baseline_commit": "7caf4e380bc8b5dc75024895ab400e770015a3ec",
        "updated_at_utc": "2026-11-06T12:00:00Z",
        "umbrella_result": "PARTIAL",
        "scoreboard": {"total": 60, "verified_pass": 0, "PARTIAL": 0,
                       "FAIL": 0, "NOT_RUN": 60, "BLOCKED": 0},
        "steps": steps,
    }


def prepare_root(destination: Path) -> Path:
    for relative in ("docs/spec/source/MJobs_Startup_Prompt.original.md",
                     "docs/spec/areas/delivery_control.md",
                     "docs/evidence/2026-10-11-durable-onboarding.json"):
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


def make_checkpoint() -> dict:
    return {
        "schema_version": 1,
        "task_id": "MJ-REQ-292",
        "objective": "Track the connected acceptance campaign",
        "owner": "campaign-owner",
        "branch": "codex/mj-req292-full-journey-20261106",
        "head_sha": "7caf4e380bc8b5dc75024895ab400e770015a3ec",
        "routing_state": "EXECUTE",
        "next_action": "Continue the authorized journey",
        "created_at": "2026-11-06T09:36:42.894560+00:00",
        "requirements": ["MJ-REQ-292"],
        "tests": [{"command": "whole journey", "result": "NOT_RUN"}],
        "blockers": [],
        "acceptance_ledger": "planning/acceptance/MJ-REQ-292.json",
        "scoreboard": {"verified_pass": 99, "total": 999, "note": "old snapshot"},
        "preserve_me": {"history": [1, 2, 3]},
    }


def write_fixture_ledger(root: Path, data: dict | None = None) -> Path:
    path = root / ledger.LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data if data is not None else make_ledger(root), indent=2) + "\n").encode("utf-8"))
    return path


def write_fixture_checkpoint(root: Path, data: dict | None = None) -> Path:
    path = root / ledger.DEFAULT_CHECKPOINT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data if data is not None else make_checkpoint(), indent=2) + "\n").encode("utf-8"))
    return path


def _write_json_lf(path: Path, value: dict) -> tuple[str, bytes]:
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), raw


def build_runtime_proof(root: Path, data: dict, step_numbers: list[int], monkeypatch,
                        *, producer_recovery_path: str | None = None) -> None:
    """Create typed local fixture evidence; this does not claim real execution."""
    from copy import deepcopy

    from cli import acceptance_journey as journey

    checks = dict(journey.REQUIRED_CHECKS)
    checks.update({
        1: {"fresh_owned_stack_created", "startup_source_run_target_bound",
            "startup_images_and_containers_bound", "startup_all_services_ready",
            "startup_migration_completed"},
        3: {"first_run_wizard_completed_with_persisted_configuration"},
        11: {"same_profile_public_readonly_jobs_observed",
             "public_jobs_visible_in_same_profile_ui"},
        23: {"previously_unknown_employer_observed"},
        24: {"employer_discovery_ran_automatically"},
        25: {"official_employer_domain_verified"},
        26: {"employer_careers_page_found"},
        27: {"employer_ats_tenant_identified"},
        28: {"employer_source_enrolled_automatically"},
        29: {"employer_openings_enumerated"},
        30: {"employer_only_opening_observed"},
        31: {"employer_duplicate_sources_collapsed"},
    })
    for number in range(1, 61):
        checks.setdefault(number, {f"synthetic_check_{number}"})
    monkeypatch.setattr(journey, "REQUIRED_CHECKS", checks)
    monkeypatch.setattr(journey, "source_archive_sha256", lambda _commit: "1" * 64)
    runtime_identity = {"source_manifest_sha256": "1" * 64,
                        "images": {"api": "sha256:" + "e" * 64}}

    def validate_runtime_identity(value):
        if value != runtime_identity:
            raise ValueError("synthetic runtime identity mismatch")
        return value

    def validate_stage_lineage(artifact, identity):
        rows = artifact.get("stage_lineage", [])
        if not isinstance(rows, list):
            raise TypeError("synthetic stage_lineage must be a list")
        indexed = {row["step"]: row for row in rows}
        if len(indexed) != len(rows):
            raise ValueError("duplicate synthetic stage lineage")
        for number, row in indexed.items():
            if (not 36 <= number <= 53
                    or any(row.get(key) != identity.get(key)
                           for key in ("profile_id", "application_id", "job_id"))):
                raise ValueError("synthetic stage lineage scope mismatch")
        return indexed

    def validate_monitoring_result(stage, artifact, baseline, run_id,
                                   *, preparation=None, recovery_evidence=None):
        if (artifact.get("stage") != ("prepare" if stage == "monitor_prepare" else "after_restart")
                or artifact.get("source") != baseline["source"]
                or artifact.get("acceptance_run_id") != run_id
                or artifact.get("external_submission_enabled") is not False
                or artifact.get("baseline_sha256") != journey.digest(baseline)):
            raise ValueError("synthetic monitoring evidence binding mismatch")
        if stage == "monitor_prepare":
            if artifact.get("observations") != []:
                raise ValueError("synthetic preparation cannot emit acceptance observations")
        elif (preparation is None or recovery_evidence is None
              or artifact.get("preparation_sha256") != journey.digest(preparation)
              or artifact.get("recovery_evidence_sha256") != journey.digest(recovery_evidence)):
            raise ValueError("synthetic monitoring restart lineage mismatch")
        return artifact

    def validate_startup_receipt(receipt, source, run_id, target, runtime):
        if (receipt.get("source") != source or receipt.get("acceptance_run_id") != run_id
                or receipt.get("target") != target or receipt.get("runtime") != runtime):
            raise ValueError("synthetic startup receipt binding mismatch")
        return receipt["observation"]

    join_output = {}

    def join_public_browser(_baseline, public, browser, public_sha, browser_sha, _runtime):
        if (browser.get("public_sha256") != public_sha
                or browser.get("run_id") != public.get("acceptance_run_id")):
            raise ValueError("synthetic public browser join mismatch")
        return join_output["joined"]

    real_numbered_observations = journey.numbered_observations

    def numbered_observations(artifact, *, segment, run_id, source, identity):
        if segment == "startup":
            rows = artifact.get("acceptance_observations")
            if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("step") != 1
                    or rows[0].get("source_commit") != source["commit"]
                    or rows[0].get("run_id") != run_id):
                raise ValueError("synthetic startup observation mismatch")
            return rows
        if segment == "public_browser":
            rows = artifact.get("acceptance_observations")
            if (not isinstance(rows, list) or {row.get("step") for row in rows} != {11, 16}
                    or any(row.get("source_commit") != source["commit"]
                           or row.get("run_id") != run_id for row in rows)):
                raise ValueError("synthetic public browser rows are stale")
            return rows
        if segment == "public":
            rows = artifact.get("acceptance_observations")
            if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("step") != 11
                    or artifact.get("source") != source
                    or artifact.get("acceptance_run_id") != run_id):
                raise ValueError("synthetic public-source observation mismatch")
            return rows
        if segment == "ui":
            output = []
            for row in artifact.get("acceptance_observations", []):
                if 23 <= row.get("step", 0) <= 31:
                    if (row.get("run_id") != run_id or row.get("source_commit") != source["commit"]
                            or {check.get("name") for check in row.get("checks", [])} != checks[row["step"]]
                            or row.get("continuity", {}).get("operation_id") != "employer-operation-1"
                            or row.get("continuity", {}).get("request_id") != "employer-request-1"):
                        raise ValueError("synthetic employer discovery observation mismatch")
                    output.append(row)
                else:
                    output.extend(real_numbered_observations(
                        {**artifact, "acceptance_observations": [row]}, segment=segment,
                        run_id=run_id, source=source, identity=identity))
            return output
        if segment != "monitor_after":
            return real_numbered_observations(artifact, segment=segment, run_id=run_id,
                                              source=source, identity=identity)
        rows = artifact.get("observations")
        if (not isinstance(rows, list) or artifact.get("source") != source
                or artifact.get("acceptance_run_id") != run_id):
            raise ValueError("synthetic monitoring rows are stale")
        for row in rows:
            number = row.get("step")
            if (number not in {59, 60} or row.get("status") not in journey.STATUSES
                    or row.get("run_id") != run_id or row.get("source_commit") != source["commit"]
                    or {check.get("name") for check in row.get("checks", [])} != checks[number]
                    or not all(check.get("passed") is True for check in row.get("checks", []))):
                raise ValueError("synthetic monitoring row is not a named positive observation")
        return rows

    class MonitoringModule:
        @staticmethod
        def monitoring_recovery_baseline(original, preparation, preparation_sha):
            updated = deepcopy(original)
            updated["monitoring_preparation"] = {
                "artifact": "monitor_prepare.json", "artifact_sha256": preparation_sha,
                "monitoring_binding": preparation["monitoring_binding"],
                "refresh_operation": preparation["refresh_operation"],
                "refresh_receipt": preparation["refresh_receipt"]}
            return updated

    monkeypatch.setattr(journey, "validate_runtime_identity", validate_runtime_identity, raising=False)
    monkeypatch.setattr(journey, "validate_stage_lineage", validate_stage_lineage, raising=False)
    monkeypatch.setattr(journey, "validate_monitoring_result", validate_monitoring_result, raising=False)
    monkeypatch.setattr(journey, "validate_startup_receipt", validate_startup_receipt, raising=False)
    monkeypatch.setattr(journey, "join_public_browser", join_public_browser, raising=False)
    monkeypatch.setattr(journey, "validate_public_artifact", lambda *_args, **_kwargs: True,
                        raising=False)
    monkeypatch.setattr(journey, "numbered_observations", numbered_observations)
    monkeypatch.setattr(journey, "_monitoring_module", lambda: MonitoringModule(), raising=False)

    source = {"commit": "a" * 40, "tree": "b" * 40,
              "working_tree_diff_sha256": hashlib.sha256(b"").hexdigest(),
              "working_tree_status_sha256": hashlib.sha256(b"").hexdigest()}
    run_id = "runtime-proof-test-1"
    run_root = root / "docs/evidence/runtime-proof"
    report_path = run_root / "journey.json"
    timestamp = "2026-01-01T00:00:00+00:00"
    ui_baseline = {
        "id": "fixture", "name": "Alex Fixture", "email": "alex@example.test",
        "profile_id": "profile-1", "application_id": "application-1",
        "operation_id": "operation-1", "request_id": "request-1",
        "source": source, "acceptance_run_id": run_id, "timestamp": timestamp,
        "result": "PASS", "mode": "ONBOARDING_FIXTURE",
        "target_origin": "http://127.0.0.1:18777", "external_submission_enabled": False,
        "observed_job_ids": ["browse-job-1", "mock-job-1"],
        "selected_application_job_id": "mock-job-1",
        "application_document_ids": ["resume-1", "base-1", "tailored-1", "letter-1"],
        "confirmation": {"application_id": "application-1", "receipt": "fixture-receipt",
                         "submission_count": 1},
        "local_persistence": {"schema_version": 1, "domains": {
            name: {"count": 1, "sha256": "b" * 64} for name in journey.persistence.DOMAINS}},
        "runtime_identity": runtime_identity,
        "employer_discovery_lineage": {
            "profile_id": "profile-1", "operation_id": "employer-operation-1",
            "request_id": "employer-request-1", "job_ids": ["browse-job-1"]},
    }

    def inventory(job_ids):
        jobs = [{"job_id": job_id, "canonical_sha256": hashlib.sha256(job_id.encode()).hexdigest(),
                 "source_copy_count": 1,
                 "source_copies_sha256": hashlib.sha256((job_id + "-copies").encode()).hexdigest()}
                 for job_id in job_ids]
        return {"schema_version": journey.recovery.JOB_INVENTORY_VERSION,
                "profile_id": "profile-1", "job_count": len(jobs),
                "source_copy_count": len(jobs), "jobs": jobs,
                "sha256": journey.recovery._inventory_digest(jobs)}

    ui_baseline["job_inventory"] = inventory(["mock-job-1"])
    original_identity = journey._identity
    def synthetic_identity(baseline):
        result = original_identity(baseline)
        result["employer_discovery_lineage"] = baseline["employer_discovery_lineage"]
        return result
    monkeypatch.setattr(journey, "_identity", synthetic_identity)
    identity = journey._identity(ui_baseline)
    stage_lineage = [
        {"step": number, "operation_id": "operation-1", "request_id": "request-1",
         "operation_kind": "PREPARE" if number < 44 else "APPLY_MOCK",
         "application_id": "application-1", "profile_id": "profile-1", "job_id": "mock-job-1"}
        for number in step_numbers if 36 <= number <= 53
    ]

    def continuity(number):
        if 23 <= number <= 31:
            return {"profile_id": "profile-1", "operation_id": "employer-operation-1",
                    "request_id": "employer-request-1", "job_id": "browse-job-1"}
        identifiers = (journey.IDENTIFIERS if not 17 <= number <= 22
                       else ("profile_id",))
        value = {key: ui_baseline[key] for key in identifiers}
        if 17 <= number <= 22:
            value["job_id"] = "browse-job-1"
        elif number >= 32:
            value["job_id"] = "mock-job-1"
        if number in {33, 34}:
            value["document_ids"] = ["base-1", "tailored-1"]
        elif number == 35:
            value["document_id"] = "letter-1"
        return value

    segments = {name: [] for name in ("ui", "public", "monitor_prepare", "recovery", "monitor_after")}
    for number in step_numbers:
        if number == 1:
            continue
        evidence_kind = "PUBLIC_READ_ONLY" if number == 11 else "CONTROLLED_FIXTURE"
        observation = {
            "step": number, "status": "PASS",
            "observable_result": f"Observed the numbered result for step {number}.",
            "evidence_kind": evidence_kind, "source_commit": source["commit"],
            "run_id": run_id, "continuity": continuity(number),
            "checks": [{"name": name, "passed": True} for name in sorted(checks[number])],
        }
        if number in {11, 16}:
            if number == 11:
                observation["checks"] = [{"name": "same_profile_public_readonly_jobs_observed",
                                          "passed": True}]
                segments["public"].append(observation)
            continue
        segment = ("recovery" if 54 <= number <= 58
                   else "monitor_after" if number in {59, 60} else "ui")
        segments[segment].append(observation)

    public_artifact = None
    if segments["public"]:
        public_artifact = {
            **{key: ui_baseline[key] for key in journey.IDENTIFIERS},
            "timestamp": timestamp, "source": source, "acceptance_run_id": run_id,
            "result": "PASS", "provider": "wwr", "execution_scope": "LIVE_PUBLIC_READ_ONLY",
            "external_submission_enabled": False, "observed_real_job_ids": ["real-job-1"],
            "selected_application_job_id": "mock-job-1",
            "selected_application_route_id": "mock-route-1",
            "cleanup": {"status": "RESTORED_DISABLED"},
            "acceptance_observations": segments["public"],
        }
        public_path = run_root / "public.json"
        public_sha, _ = _write_json_lf(public_path, public_artifact)
    else:
        public_sha = None

    runtime = {"source_manifest_sha256": "1" * 64, "dockerfile_sha256": "d" * 64,
               "images": runtime_identity["images"], "compose_files": ["compose.yml"],
               "frontend_assets": {"index.html": "c" * 64}}
    target = {"project_name": "mjobs-fixture-123456789abc", "port": 18777,
              "base_url": "http://127.0.0.1:18777"}
    startup_observation = {
        "step": 1, "status": "PASS", "observable_result": "Owned startup receipt validated.",
        "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": source["commit"],
        "run_id": run_id, "continuity": {},
        "checks": [{"name": name, "passed": True} for name in sorted(checks[1])],
    }
    startup_command = ["docker", "compose", "-p", target["project_name"],
                       "-f", "compose.yml", "up", "-d"]
    startup_runtime = {**runtime, "containers": {}, "container_created_at": {},
                       "service_started_at": {}, "health": {}, "migration": {}}
    startup_receipt = {
        "schema_version": 1, "result": "PASS", "source": source,
        "acceptance_run_id": run_id, "target": target,
        "preexisting_container_ids": [], "preexisting_volume_ids": [],
        "command_exit_code": 0, "command": startup_command,
        "started_at_utc": "2026-01-01T00:00:00+00:00",
        "completed_at_utc": timestamp, "runtime": startup_runtime,
        "observation": startup_observation,
    }
    startup_receipt_path = run_root / "startup-receipt.json"
    startup_receipt_sha, _ = _write_json_lf(startup_receipt_path, startup_receipt)
    startup_payload = {
        "schema_version": 1, "result": "PASS", "scope": "OWNED_FIXTURE_STARTUP",
        "source": source, "acceptance_run_id": run_id, "timestamp": timestamp,
        "receipt_artifact": startup_receipt_path.name, "receipt_sha256": startup_receipt_sha,
        "runtime": startup_runtime, "acceptance_observations": [startup_observation],
    }
    startup_path = run_root / "startup.json"
    startup_sha, _ = _write_json_lf(startup_path, startup_payload)
    startup_ref = {"artifact": startup_path.name, "sha256": startup_sha}

    public_browser_ref = public_observations_ref = None
    joined_public_rows = []
    if public_artifact is not None:
        public_ids = {key: ui_baseline[key] for key in journey.IDENTIFIERS}
        if 11 in step_numbers:
            joined_public_rows.append({
                "step": 11, "status": "PASS",
                "observable_result": "Collected public job visible in same profile UI.",
                "evidence_kind": "PUBLIC_READ_ONLY", "source_commit": source["commit"],
                "run_id": run_id, "continuity": public_ids,
                "checks": [{"name": name, "passed": True} for name in sorted(checks[11])],
            })
        if 16 in step_numbers:
            joined_public_rows.append({
                "step": 16, "status": "PASS",
                "observable_result": "Provider remote restriction visible.",
                "evidence_kind": "PUBLIC_READ_ONLY", "source_commit": source["commit"],
                "run_id": run_id, "continuity": public_ids,
                "checks": [{"name": "source_remote_restriction_visible", "passed": True}],
            })
        browser_payload = {"schema_version": 1, "result": "PASS", "source": source,
                           "acceptance_run_id": run_id, "run_id": run_id,
                           "public_sha256": public_sha,
                           "timestamp_utc": timestamp}
        browser_path = run_root / "public-browser.json"
        browser_sha, _ = _write_json_lf(browser_path, browser_payload)
        joined_payload = {
            "schema_version": 1, "result": "PARTIAL", "timestamp": timestamp,
            "source": source, "acceptance_run_id": run_id,
            "public_artifact": "public.json", "public_sha256": public_sha,
            "browser_artifact": "public-browser.json", "browser_sha256": browser_sha,
            "runtime_identity": runtime_identity, "acceptance_observations": joined_public_rows,
        }
        join_output["joined"] = joined_payload
        joined_path = run_root / "public-observations.json"
        joined_sha, _ = _write_json_lf(joined_path, joined_payload)
        public_browser_ref = {"artifact": browser_path.name, "sha256": browser_sha}
        public_observations_ref = {"artifact": joined_path.name, "sha256": joined_sha}

    segment_refs = {}
    ui_payload = {**deepcopy(ui_baseline), "acceptance_observations": segments["ui"],
                  "stage_lineage": stage_lineage}
    ui_path = run_root / "ui.json"
    ui_sha, _ = _write_json_lf(ui_path, ui_payload)
    segment_refs["ui"] = {"status": "COMPLETE", "artifact": ui_path.name, "sha256": ui_sha}
    if public_artifact is not None:
        segment_refs["public"] = {"status": "COMPLETE", "artifact": "public.json",
                                   "sha256": public_sha}
    derived_ref = None
    monitoring_source_baseline = ui_payload
    if public_artifact is not None and any(54 <= number <= 58 for number in step_numbers):
        derived = deepcopy(ui_payload)
        derived["job_inventory"] = inventory(["mock-job-1", "real-job-1"])
        derived["baseline_lineage"] = {"ui_sha256": ui_sha, "public_sha256": public_sha}
        derived["baseline_recaptured_at_utc"] = timestamp
        derived_path = run_root / "recovery-baseline.json"
        derived_sha, _ = _write_json_lf(derived_path, derived)
        derived_ref = {"artifact": derived_path.name, "sha256": derived_sha}
        monitoring_source_baseline = derived
    monitoring_enabled = bool({59, 60}.intersection(step_numbers))
    monitoring_binding = {
        "source_id": "source-1", "source_version": 1, "profile_id": "profile-1",
        "profile_version": 1, "marker": "direct-company-source-v1",
        "origin": "fixture", "search_fingerprint": "a" * 64, "revision": 1,
        "enabled": True, "poll_interval": 300, "external_submission_enabled": False,
        "next_due": "2026-01-01T00:30:00+00:00",
    }
    refresh_operation = {"id": "refresh-1", "state": "COMPLETED", "attempt_count": 1,
                        "started_at": "2026-01-01T00:01:00+00:00",
                        "completed_at": "2026-01-01T00:02:00+00:00", "params": {}}
    refresh_receipt = {"kind": "PUBLIC_COMPLETED_OPERATION_POINTER", "profile_id": "profile-1",
                       "source_id": "source-1", "operation_id": "refresh-1"}
    monitor_payload = {
        "timestamp": timestamp, "source": source, "acceptance_run_id": run_id,
        "profile_id": "profile-1", "application_id": "application-1",
        "operation_id": "operation-1", "request_id": "request-1",
        "result": "PARTIAL", "external_submission_enabled": False,
        "monitoring_binding": monitoring_binding, "refresh_operation": refresh_operation,
        "refresh_receipt": refresh_receipt, "job_inventory": monitoring_source_baseline["job_inventory"],
        "local_persistence": monitoring_source_baseline["local_persistence"],
    }
    preparation_payload = {**monitor_payload, "stage": "prepare", "stage_result": "PASS",
                          "baseline_sha256": journey.digest(monitoring_source_baseline), "observations": []}
    preparation_ref = None
    monitoring_baseline_ref = None
    monitoring_baseline = monitoring_source_baseline
    if monitoring_enabled:
        prep_path = run_root / "monitor_prepare.json"
        prep_sha, _ = _write_json_lf(prep_path, preparation_payload)
        preparation_ref = {"status": "COMPLETE", "artifact": prep_path.name, "sha256": prep_sha}
        monitoring_baseline = deepcopy(monitoring_source_baseline)
        monitoring_baseline["monitoring_preparation"] = {
            "artifact": "monitor_prepare.json", "artifact_sha256": prep_sha,
            "monitoring_binding": monitoring_binding, "refresh_operation": refresh_operation,
            "refresh_receipt": refresh_receipt}
        monitoring_baseline_path = run_root / "monitoring-baseline.json"
        monitoring_baseline_sha, _ = _write_json_lf(monitoring_baseline_path, monitoring_baseline)
        monitoring_baseline_ref = {"artifact": monitoring_baseline_path.name,
                                   "sha256": monitoring_baseline_sha}

    for name in ("ui", "public", "monitor_prepare", "recovery", "monitor_after"):
        if not segments[name] and not (name == "ui" and step_numbers):
            if name == "monitor_prepare" and monitoring_enabled:
                segment_refs[name] = preparation_ref
            continue
        if name == "ui":
            payload = {**deepcopy(ui_baseline), "acceptance_observations": segments[name],
                       "stage_lineage": stage_lineage}
        elif name == "public":
            payload = public_artifact
        elif name == "monitor_prepare":
            payload = preparation_payload
        else:
            effective_baseline = monitoring_baseline if monitoring_enabled else monitoring_source_baseline
            recovered_inventory = effective_baseline["job_inventory"]
            compose_target = {"project_name": "mjobs-fixture-123456789abc", "port": 18777,
                              "profile": "fixture", "project_directory": str(journey.ROOT.resolve()),
                              "compose_files": ["compose.yml"]}
            raw_payload = {
                "timestamp": timestamp, "source": source, "acceptance_run_id": run_id,
                "application_id": "application-1", "result": "PASS",
                "external_submission_enabled": False, "compose_target": compose_target,
                "public_job_inventory_recovery": {"result": "PASS",
                    "profile_id": recovered_inventory["profile_id"],
                    "sha256": recovered_inventory["sha256"],
                    "job_count": recovered_inventory["job_count"],
                    "source_copy_count": recovered_inventory["source_copy_count"]},
                "local_persistence": {"result": "PASS", "domains": list(journey.persistence.DOMAINS)},
                "container_health": {"after": {"api": {"status": "running", "health": "healthy"}}},
            }
            if name == "recovery":
                raw_path = run_root / "compose-recovery.json"
                raw_sha, _ = _write_json_lf(raw_path, raw_payload)
                payload = {
                    **deepcopy(raw_payload), "schema_version": 1,
                    "scope": "CONTROLLED_RECOVERY_SEGMENT",
                    **{key: ui_baseline[key] for key in journey.IDENTIFIERS},
                    "completed_at_utc": timestamp, "recovery_artifact": raw_path.name,
                    "recovery_artifact_sha256": raw_sha,
                    "recovery_artifact_path": (producer_recovery_path
                        or "Z:/producer-host/compose-recovery.json"),
                    "baseline_sha256": journey.digest(effective_baseline),
                    "acceptance_observations": segments[name],
                }
            else:
                payload = {**monitor_payload, "stage": "after_restart", "stage_result": "PASS",
                           "baseline_sha256": journey.digest(monitoring_source_baseline),
                           "preparation_sha256": journey.digest(preparation_payload),
                           "recovery_evidence_sha256": "pending",
                           "observations": segments[name], "stage_lineage": stage_lineage}
                recovery_payload = None
                if "recovery" in segment_refs:
                    recovery_payload = json.loads((run_root / "recovery.json").read_text(encoding="utf-8"))
                if recovery_payload is None:
                    # The after-restart proof always follows a completed recovery segment.
                    raise AssertionError("synthetic monitor_after lacks recovery producer data")
                payload["recovery_evidence_sha256"] = journey.digest(recovery_payload)
        path = run_root / f"{name}.json"
        segment_sha, _ = _write_json_lf(path, payload)
        segment_refs[name] = {"status": "COMPLETE", "artifact": path.name,
                              "sha256": segment_sha}

    statuses = ["PASS" if number in step_numbers else "NOT_RUN" for number in range(1, 61)]
    journey_steps = [{"number": number, "status": status} for number, status in enumerate(statuses, 1)]
    observation_by_step = {row["step"]: row for rows in segments.values() for row in rows}
    observation_by_step.update({row["step"]: row for row in joined_public_rows})
    for number in step_numbers:
        segment = ("startup" if number == 1 else "public_browser" if number in {11, 16}
                   else "recovery" if 54 <= number <= 58
                   else "monitor_after" if number in {59, 60} else "ui")
        observation = startup_observation if number == 1 else observation_by_step[number]
        evidence_ref = (startup_ref if segment == "startup" else
                        public_observations_ref if segment == "public_browser" else
                        segment_refs[segment])
        verification = {
            "tested_commit": source["commit"], "evidence_artifact": evidence_ref["artifact"],
            "evidence_sha256": evidence_ref["sha256"], "run_id": run_id,
            "evidence_kind": observation["evidence_kind"],
            "command": startup_command if segment == "startup" else
                       "python -m cli.acceptance_journey (synthetic verifier fixture)",
            "observed_result": observation["observable_result"],
            "executed_at_utc": timestamp, "continuity": observation["continuity"],
            **({"job_scope": "SAME_PROFILE_BROWSED_JOB"} if 17 <= number <= 22 else {}),
            **({"job_scope": "SELECTED_APPLICATION_JOB"} if 32 <= number <= 53 else {}),
        }
        if 36 <= number <= 53:
            verification["stage_lineage"] = next(row for row in stage_lineage if row["step"] == number)
        if 23 <= number <= 31:
            verification["employer_discovery_lineage"] = ui_baseline["employer_discovery_lineage"]
        journey_steps[number - 1] = {
            "number": number, "status": "PASS", "observation": observation,
            "verification": verification,
        }

    report = {
        "schema_version": 1, "requirement_id": "MJ-REQ-292", "run_id": run_id,
        "source": source, "target": {"project_name": "mjobs-fixture-123456789abc", "port": 18777,
                                       "base_url": "http://127.0.0.1:18777"},
        "started_at_utc": timestamp, "updated_at_utc": timestamp,
        "result": "PASS" if set(statuses) == {"PASS"} else "PARTIAL",
        "review_status": "NOT_RUN", "continuity": identity,
        "steps": journey_steps, "segments": segment_refs,
        "runtime_identity": runtime_identity, "stage_lineage": stage_lineage,
        "employer_discovery_lineage": ui_baseline["employer_discovery_lineage"],
        "scoreboard": {"observed_PASS": statuses.count("PASS"),
                       "PARTIAL": statuses.count("PARTIAL"),
                       "FAIL": statuses.count("FAIL"),
                       "NOT_RUN": statuses.count("NOT_RUN"),
                       "BLOCKED": statuses.count("BLOCKED")},
        "runtime": runtime,
        "public_source_provider": "wwr" if public_artifact is not None else None,
        "frontend_build": {"status": "PASS", "receipt_sha256": "0" * 64,
                           "receipt": {"schema_version": 1, "result": "PASS",
                               "source_commit": source["commit"], "source_tree": source["tree"],
                               "source_archive_sha256": "1" * 64,
                               "dockerfile_sha256": "d" * 64,
                               "image_id": "sha256:" + "e" * 64,
                               "frontend_assets": {"index.html": "c" * 64},
                               "build_command": ["docker", "build"],
                               "completed_at_utc": timestamp}},
    }
    report["frontend_build"]["receipt_sha256"] = journey.digest(report["frontend_build"]["receipt"])
    if derived_ref is not None:
        report["derived_baseline"] = derived_ref
    if monitoring_enabled:
        report["monitoring_input_baseline"] = {
            "artifact": (derived_ref or segment_refs["ui"])["artifact"],
            "sha256": (derived_ref or segment_refs["ui"])["sha256"]}
        report["monitoring_baseline"] = monitoring_baseline_ref
    if 1 in step_numbers:
        report["startup"] = startup_ref
    if public_browser_ref is not None and public_observations_ref is not None:
        report["public_browser"] = public_browser_ref
        report["public_observations"] = public_observations_ref
    report_sha, _ = _write_json_lf(report_path, report)
    report_relative = report_path.relative_to(root).as_posix()
    data["journey_report"] = {"artifact": report_relative, "sha256": report_sha}
    for number in step_numbers:
        step = data["steps"][number - 1]
        segment = ("startup" if number == 1 else "public_browser" if number in {11, 16}
                   else "recovery" if 54 <= number <= 58
                   else "monitor_after" if number in {59, 60} else "ui")
        evidence_ref = (startup_ref if segment == "startup" else
                        public_observations_ref if segment == "public_browser" else
                        segment_refs[segment])
        step["status"] = "PASS"
        step["missing_behavior"] = ""
        step["verification"].update(journey_steps[number - 1]["verification"])
        step["verification"]["observed_result"] = "PASS"
        step["verification"]["evidence_artifact"] = (
            run_root / evidence_ref["artifact"]).relative_to(root).as_posix()
        step["verification"]["continuity"] = journey_steps[number - 1]["observation"]["continuity"]
        review_path = run_root / "review.json"
        merge_path = run_root / "merge.json"
        # Receipts are written once below; these paths/hashes are filled after their bytes exist.

    commit = source["commit"]
    review = {"schema_version": 1, "requirement_id": "MJ-REQ-292", "result": "APPROVED",
              "reviewer": {"name": "independent-reviewer", "role": "independent_local_review",
                           "authored_source": False},
              "tested_commit": commit, "evidence_sha256": report_sha,
              "approved_steps": sorted(step_numbers), "captured_at_utc": "2026-01-01T00:10:00Z",
              "scope_limits": ["Local typed receipt content; live GitHub API state not queried"]}
    review_path = run_root / "review.json"
    review_sha, _ = _write_json_lf(review_path, review)
    contexts = ["CI / required", "Integration / PostgreSQL and Redis",
                "Disposable Compose / mock end-to-end", "Dependencies / introduced vulnerabilities",
                "Security / secret scan"]
    merge = {"schema_version": 1, "requirement_id": "MJ-REQ-292", "result": "MERGED",
             "pr_url": "https://github.com/MulattoTech/MJobs/pull/1001",
             "head_commit": "e" * 40, "base_commit": "d" * 40,
             "merged_commit": commit, "tested_commit": commit,
             "checks": [{"name": name, "result": "SUCCESS", "head_commit": "e" * 40,
                         "url": "https://github.com/MulattoTech/MJobs/actions/runs/1001"}
                        for name in contexts],
             "unresolved_threads": 0, "captured_at_utc": "2026-01-01T00:20:00Z"}
    merge_path = run_root / "merge.json"
    merge_sha, _ = _write_json_lf(merge_path, merge)
    for number in step_numbers:
        step = data["steps"][number - 1]
        step["review"] = {"status": "PASS", "evidence_artifact": review_path.relative_to(root).as_posix(),
                          "evidence_sha256": review_sha}
        step["merge"] = {"status": "MERGED", "pr_url": merge["pr_url"],
                         "integrated_commit": commit,
                         "evidence_artifact": merge_path.relative_to(root).as_posix(),
                         "evidence_sha256": merge_sha}
    counts = {status: statuses.count(status) for status in ledger.STATUSES}
    data["scoreboard"] = {"total": 60, "verified_pass": counts["PASS"],
                           **{status: counts[status] for status in ledger.STATUSES if status != "PASS"}}


def test_initial_ledger_validates_and_reports_recomputed_x_of_60():
    report = ledger.validate_ledger(make_ledger(), ROOT)
    assert report.errors == []
    assert report.counts == {"PASS": 0, "PARTIAL": 0, "FAIL": 0,
                             "NOT_RUN": 60, "BLOCKED": 0}
    assert ledger.render_report(report) == (
        "MJ-REQ-292: 0/60 recorded PASS claims; structural validation only "
        "(runtime/review/integration not certified); 0 PARTIAL, 0 FAIL, 60 NOT_RUN, 0 BLOCKED"
    )


@pytest.mark.parametrize(("step", "segment"), [
    (1, "startup"), (16, "public_browser"), (36, "ui"), (53, "ui"),
    (54, "recovery"), (58, "recovery"), (59, "monitor_after"),
    (60, "monitor_after"), (11, "public_browser"),
])
def test_latest_journey_steps_join_their_producer_segment(step, segment):
    assert ledger._segment_for_step(step) == segment


@pytest.mark.parametrize("name", [
    "ui.json", "public.json", "monitor_prepare.json", "recovery.json",
    "monitor_after.json", "recovery-baseline.json", "monitoring-baseline.json",
    "startup.json", "startup-receipt.json", "public-browser.json", "public-observations.json",
    "compose-recovery.json",
])
def test_latest_journey_bundle_accepts_only_known_sibling_artifact_names(name):
    assert ledger._is_known_bundle_artifact(name)


@pytest.mark.parametrize("name", [
    "../ui.json", "C:/private/recovery.json", "/tmp/ui.json", "other.json",
    "subdir/monitor_after.json", "..\\compose-recovery.json",
])
def test_latest_journey_bundle_rejects_paths_and_unknown_names(name):
    assert not ledger._is_known_bundle_artifact(name)


def test_tracked_json_loader_requires_published_utf8_lf_bytes(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_bytes(b'{\r\n  "schema_version": 1\r\n}\r\n')
    with pytest.raises(ValueError, match="canonical UTF-8 LF"):
        ledger.load_ledger(path)

    # General parser still accepts platform-independent JSON bytes; its caller
    # must choose a canonical tracked file policy explicitly.
    assert ledger.load_ledger_bytes(b'{\r\n  "schema_version": 1\r\n}\r\n') == {
        "schema_version": 1}


@pytest.mark.parametrize("mutation", [
    lambda data: data.__setitem__("issue_url", "https://example.invalid/293"),
    lambda data: data["specification"].__setitem__("original_lines", [1, 2]),
    lambda data: data["steps"][0].__setitem__("phase", "G"),
    lambda data: data["steps"][0].__setitem__("ownership_status", ""),
    lambda data: data["steps"][0]["evidence_of_existing_behavior"][0].__setitem__(
        "classification", "CURRENT_PASS"
    ),
])
def test_known_metadata_is_validated_and_historical_pointers_do_not_promote(mutation):
    data = make_ledger()
    mutation(data)
    assert ledger.validate_ledger(data, ROOT).errors
    clean = make_ledger()
    assert clean["steps"][0]["status"] == "NOT_RUN"
    assert clean["scoreboard"]["verified_pass"] == 0


@pytest.mark.parametrize("mutation", [
    lambda data: data["steps"][0].__setitem__("phase", []),
    lambda data: data["steps"][0]["review"].__setitem__("status", []),
    lambda data: data["steps"][0]["merge"].__setitem__("status", []),
    lambda data: data["steps"][0]["verification"].__setitem__("evidence_kind", []),
    lambda data: data.__setitem__("acceptance_policy", []),
])
def test_malformed_optional_types_are_rejected_without_crashing(mutation):
    data = make_ledger()
    mutation(data)
    report = ledger.validate_ledger(data, ROOT)
    assert report.errors


@pytest.mark.parametrize("mutation", [
    lambda data: data["steps"].pop(),
    lambda data: data["steps"].__setitem__(1, deepcopy(data["steps"][0])),
    lambda data: data["steps"][0].__setitem__("original_requirement", "edited text"),
    lambda data: data["scoreboard"].__setitem__("NOT_RUN", 59),
    lambda data: data["scoreboard"].__setitem__("verified_pass", 1),
    lambda data: data["steps"][0].__setitem__("status", "PASS"),
])
def test_missing_duplicate_drifted_or_contradictory_records_fail_closed(mutation):
    data = make_ledger()
    mutation(data)
    report = ledger.validate_ledger(data, ROOT)
    assert report.errors


def test_delivery_control_section_must_match_original_source_exactly(tmp_path):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    original = root / "docs/spec/areas/delivery_control.md"
    changed = original
    text = original.read_text(encoding="utf-8-sig")
    changed.write_text(text.replace("1. Start the full stack.", "1. Start a partial stack."),
                       encoding="utf-8")
    assert ledger.validate_ledger(data, tmp_path).errors


def passing_step(data: dict, number: int, root: Path, *, evidence_kind="CONTROLLED_FIXTURE"):
    relative = f"docs/evidence/ledger-fixture-{number}.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"fixture_kind": "unit-only", "purpose": "artifact hash validation"}).encode()
    path.write_bytes(payload)
    step = data["steps"][number - 1]
    step["status"] = "PASS"
    step["missing_behavior"] = ""
    step["verification"].update({
        "tested_commit": "1" * 40,
        "evidence_artifact": relative,
        "evidence_sha256": hashlib.sha256(payload).hexdigest(),
        "command": "python -m pytest tests/unit/example.py -q",
        "observed_result": "PASS",
        "executed_at_utc": "2026-11-06T12:00:00Z",
        "run_id": f"local-{number}",
        "evidence_kind": evidence_kind,
        "continuity": {"previous_run_id": "prior-local-run",
                       "same_environment": True},
    })
    return step


def test_pass_claim_requires_current_artifact_hash_source_sha_and_continuity(tmp_path):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    passing_step(data, 1, root)
    data["scoreboard"].update({"verified_pass": 1, "NOT_RUN": 59})
    report = ledger.validate_ledger(data, root)
    assert report.errors == []
    assert report.counts["PASS"] == 1
    assert "1/60 recorded PASS claims" in ledger.render_report(report)

    data["steps"][0]["verification"]["evidence_sha256"] = "0" * 64
    report = ledger.validate_ledger(data, root)
    assert report.errors

    data["steps"][0]["verification"]["evidence_sha256"] = hashlib.sha256(
        (root / data["steps"][0]["verification"]["evidence_artifact"]).read_bytes()
    ).hexdigest()
    data["steps"][0]["verification"]["command"] = "TBD"
    assert ledger.validate_ledger(data, root).errors

    data["steps"][0]["verification"]["command"] = "python -m pytest tests/unit/example.py -q"
    data["steps"][0]["verification"]["evidence_sha256"] = None
    assert ledger.validate_ledger(data, root).errors


def test_controlled_fixture_does_not_satisfy_public_step_11(tmp_path):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    passing_step(data, 11, root, evidence_kind="CONTROLLED_FIXTURE")
    report = ledger.validate_ledger(data, root)
    assert report.errors


def test_umbrella_pass_claim_fails_without_all_steps_and_separate_review_merge(tmp_path):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    passing_step(data, 1, root)
    data["scoreboard"].update({"verified_pass": 1, "NOT_RUN": 59})
    data["umbrella_result"] = "PASS"
    report = ledger.validate_ledger(data, root)
    assert any("cannot be certified by structural validation" in error for error in report.errors)


def test_runtime_evidence_mode_joins_one_pass_to_report_segment_review_and_merge(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    build_runtime_proof(root, data, [2], monkeypatch)

    structural = ledger.validate_ledger(data, root)
    assert structural.errors == []
    assert structural.runtime_evidence_validated is False

    verified = ledger.validate_ledger(data, root, require_runtime_evidence=True)
    assert verified.errors == []
    assert verified.runtime_evidence_validated is True
    assert verified.counts["PASS"] == 1


def test_runtime_evidence_mode_can_validate_all_60_and_remove_permanent_pass_block(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    build_runtime_proof(root, data, list(range(1, 61)), monkeypatch)
    data["umbrella_result"] = "PASS"

    structural = ledger.validate_ledger(data, root)
    assert any("cannot be certified by structural validation" in error for error in structural.errors)

    verified = ledger.validate_ledger(data, root, require_runtime_evidence=True)
    assert verified.errors == []
    assert verified.runtime_evidence_validated is True
    assert verified.counts["PASS"] == 60


def test_runtime_evidence_uses_bundled_recovery_bytes_never_producer_host_path(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    external_path = tmp_path / "producer-private" / "compose-recovery.json"
    external_path.parent.mkdir(parents=True)
    external_path.write_bytes(b"not the published recovery receipt")
    build_runtime_proof(root, data, [2, 55], monkeypatch,
                        producer_recovery_path=str(external_path))

    verified = ledger.validate_ledger(data, root, require_runtime_evidence=True)
    assert verified.errors == []
    assert verified.counts["PASS"] == 2


def test_runtime_evidence_requires_same_row_and_report_stage_lineage(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    build_runtime_proof(root, data, [2, 36], monkeypatch)
    data["steps"][35]["verification"]["stage_lineage"]["job_id"] = "other-job"

    verified = ledger.validate_ledger(data, root, require_runtime_evidence=True)
    assert any("stage lineage" in error for error in verified.errors), verified.errors


def test_runtime_evidence_fails_closed_when_bundled_raw_recovery_is_missing(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    build_runtime_proof(root, data, [2, 55], monkeypatch)
    (root / "docs/evidence/runtime-proof/compose-recovery.json").unlink()

    verified = ledger.validate_ledger(data, root, require_runtime_evidence=True)
    assert any("compose-recovery.json" in error for error in verified.errors)


def test_runtime_evidence_rejects_unallowlisted_monitoring_baseline_reference(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    build_runtime_proof(root, data, [2, 55, 59], monkeypatch)
    report_path = root / data["journey_report"]["artifact"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["monitoring_input_baseline"]["artifact"] = "../../private.json"
    report_sha, _ = _write_json_lf(report_path, report)
    data["journey_report"]["sha256"] = report_sha

    verified = ledger.validate_ledger(data, root, require_runtime_evidence=True)
    assert any("allowlisted" in error for error in verified.errors), verified.errors


@pytest.mark.parametrize("mutation", [
    "missing_journey_report", "report_hash", "source_commit", "report_step_number",
    "producer_artifact_hash", "continuity", "missing_review", "self_review",
    "review_row_scope", "missing_merge", "merge_commit", "merge_check_head",
    "missing_required_check", "merge_threads", "step55_inventory",
    "step59_recovery",
])
def test_runtime_evidence_rejects_broken_source_hash_row_review_merge_and_recovery(
        tmp_path, monkeypatch, mutation):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    steps = [2, 55, 59] if mutation in {"step55_inventory", "step59_recovery"} else [2]
    build_runtime_proof(root, data, steps, monkeypatch)
    if mutation in {"step55_inventory", "step59_recovery"}:
        # The report must include a real recovery stage; the UI baseline is
        # retained by the helper even when no UI observation earns credit.
        steps = [2, 55, 59]

    run_root = root / "docs/evidence/runtime-proof"
    report_path = run_root / "journey.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    evidence = data["steps"][1]["verification"]
    if mutation == "missing_journey_report":
        data.pop("journey_report")
    elif mutation == "report_hash":
        data["journey_report"]["sha256"] = "0" * 64
    elif mutation == "source_commit":
        report["source"]["commit"] = "b" * 40
        digest, _ = _write_json_lf(report_path, report)
        data["journey_report"]["sha256"] = digest
    elif mutation == "report_step_number":
        report["steps"][1]["number"] = 3
        digest, _ = _write_json_lf(report_path, report)
        data["journey_report"]["sha256"] = digest
    elif mutation == "producer_artifact_hash":
        evidence["evidence_sha256"] = "0" * 64
    elif mutation == "continuity":
        evidence["continuity"] = {"profile_id": "different-profile"}
    elif mutation == "missing_review":
        data["steps"][1]["review"] = {"status": "NOT_RUN", "evidence_artifact": None}
    elif mutation == "self_review":
        review_path = run_root / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["reviewer"]["authored_source"] = True
        digest, _ = _write_json_lf(review_path, review)
        data["steps"][1]["review"]["evidence_sha256"] = digest
    elif mutation == "review_row_scope":
        review_path = run_root / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["approved_steps"] = [3]
        review["evidence_sha256"] = data["journey_report"]["sha256"]
        digest, _ = _write_json_lf(review_path, review)
        data["steps"][1]["review"]["evidence_sha256"] = digest
    elif mutation == "missing_merge":
        data["steps"][1]["merge"] = {"status": "NOT_MERGED", "pr_url": None,
                                       "integrated_commit": None}
    elif mutation == "merge_commit":
        merge_path = run_root / "merge.json"
        merge = json.loads(merge_path.read_text(encoding="utf-8"))
        merge["merged_commit"] = "b" * 40
        digest, _ = _write_json_lf(merge_path, merge)
        data["steps"][1]["merge"]["evidence_sha256"] = digest
    elif mutation == "merge_check_head":
        merge_path = run_root / "merge.json"
        merge = json.loads(merge_path.read_text(encoding="utf-8"))
        merge["checks"][0]["head_commit"] = "f" * 40
        digest, _ = _write_json_lf(merge_path, merge)
        data["steps"][1]["merge"]["evidence_sha256"] = digest
    elif mutation == "missing_required_check":
        merge_path = run_root / "merge.json"
        merge = json.loads(merge_path.read_text(encoding="utf-8"))
        merge["checks"].pop()
        digest, _ = _write_json_lf(merge_path, merge)
        data["steps"][1]["merge"]["evidence_sha256"] = digest
    elif mutation == "merge_threads":
        merge_path = run_root / "merge.json"
        merge = json.loads(merge_path.read_text(encoding="utf-8"))
        merge["unresolved_threads"] = 1
        digest, _ = _write_json_lf(merge_path, merge)
        data["steps"][1]["merge"]["evidence_sha256"] = digest
    elif mutation == "step55_inventory":
        recovery_path = run_root / "recovery.json"
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        recovery["public_job_inventory_recovery"]["sha256"] = "0" * 64
        digest, _ = _write_json_lf(recovery_path, recovery)
        report["segments"]["recovery"]["sha256"] = digest
        for number in steps:
            report["steps"][number - 1]["verification"]["evidence_sha256"] = digest
        report_digest, _ = _write_json_lf(report_path, report)
        data["journey_report"]["sha256"] = report_digest
        review_path = run_root / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["evidence_sha256"] = report_digest
        review_sha, _ = _write_json_lf(review_path, review)
        for number in steps:
            data["steps"][number - 1]["verification"]["evidence_sha256"] = digest
            data["steps"][number - 1]["review"]["evidence_sha256"] = review_sha
    elif mutation == "step59_recovery":
        data["steps"][58]["verification"]["tested_commit"] = "b" * 40

    verified = ledger.validate_ledger(data, root, require_runtime_evidence=True)
    assert verified.errors
    assert verified.runtime_evidence_validated is False


def test_source_section_rejects_nonsequential_number_labels(tmp_path):
    root = prepare_root(tmp_path)
    source = root / "docs/spec/source/MJobs_Startup_Prompt.original.md"
    source.write_text(source.read_text(encoding="utf-8-sig").replace(
        "\n2. Open the dashboard.", "\n3. Open the dashboard.", 1), encoding="utf-8")
    report = ledger.validate_ledger(make_ledger(root), root)
    assert any("labels must be exactly 1 through 60" in error for error in report.errors)


def test_json_loader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"status":"NOT_RUN","status":"PASS"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key: status"):
        ledger.load_ledger(path)


def test_checkpoint_scoreboard_is_derived_and_only_projection_fields_change(tmp_path):
    root = prepare_root(tmp_path)
    write_fixture_ledger(root)
    checkpoint_path = write_fixture_checkpoint(root)
    before = json.loads(checkpoint_path.read_text(encoding="utf-8"))

    ledger.write_checkpoint_scoreboard(root)

    after = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert after["scoreboard"] == {
        "acceptance_semantics": "LEDGER_COUNTS_NOT_RUNTIME_CERTIFICATION",
        "verified_pass": 0, "total": 60, "PARTIAL": 0, "FAIL": 0,
        "NOT_RUN": 60, "BLOCKED": 0, "umbrella_result": "PARTIAL",
        "baseline_commit": "7caf4e380bc8b5dc75024895ab400e770015a3ec",
    }
    assert after["acceptance_ledger_sha256"] == hashlib.sha256(
        (root / ledger.LEDGER_PATH).read_bytes()
    ).hexdigest()
    assert b"\r" not in checkpoint_path.read_bytes()
    assert {k: v for k, v in after.items() if k not in {"scoreboard", "acceptance_ledger_sha256"}} == {
        k: v for k, v in before.items() if k not in {"scoreboard", "acceptance_ledger_sha256"}
    }
    assert ledger.check_checkpoint_scoreboard(root) == []


def test_checkpoint_projection_check_detects_ledger_and_snapshot_drift(tmp_path):
    root = prepare_root(tmp_path)
    write_fixture_ledger(root)
    checkpoint_path = write_fixture_checkpoint(root)
    ledger.write_checkpoint_scoreboard(root)
    ledger_path = root / ledger.LEDGER_PATH
    ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger_data["updated_at_utc"] = "2026-11-06T13:00:00Z"
    ledger_path.write_text(json.dumps(ledger_data), encoding="utf-8")
    checkpoint_bytes = checkpoint_path.read_bytes()
    errors = ledger.check_checkpoint_scoreboard(root)
    assert any("acceptance_ledger_sha256 differs" in error for error in errors)
    assert checkpoint_path.read_bytes() == checkpoint_bytes

    ledger.write_checkpoint_scoreboard(root)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["scoreboard"]["verified_pass"] = 1
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    checkpoint_bytes = checkpoint_path.read_bytes()
    errors = ledger.check_checkpoint_scoreboard(root)
    assert any("scoreboard differs" in error for error in errors)
    assert checkpoint_path.read_bytes() == checkpoint_bytes

    ledger.write_checkpoint_scoreboard(root)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["acceptance_ledger_sha256"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    errors = ledger.check_checkpoint_scoreboard(root)
    assert any("acceptance_ledger_sha256 differs" in error for error in errors)


def test_checkpoint_projection_refuses_invalid_ledger_without_mutating_handoff(tmp_path):
    root = prepare_root(tmp_path)
    bad = make_ledger(root)
    bad["umbrella_result"] = "PASS"
    write_fixture_ledger(root, bad)
    checkpoint_path = write_fixture_checkpoint(root)
    before = checkpoint_path.read_bytes()
    with pytest.raises(ValueError, match="structurally invalid"):
        ledger.write_checkpoint_scoreboard(root)
    assert checkpoint_path.read_bytes() == before


def test_checkpoint_projection_hashes_the_same_ledger_bytes_it_parses(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    ledger_path = write_fixture_ledger(root, data)
    checkpoint_path = write_fixture_checkpoint(root)
    original_bytes = ledger_path.read_bytes()
    changed = json.loads(original_bytes)
    changed["steps"][0]["status"] = "PARTIAL"
    changed["scoreboard"].update({"PARTIAL": 1, "NOT_RUN": 59})
    changed["updated_at_utc"] = "2026-11-06T14:00:00Z"
    replacement = json.dumps(changed).encode()
    real_read_bytes = Path.read_bytes
    changed_once = False

    def read_then_change(path):
        nonlocal changed_once
        raw = real_read_bytes(path)
        if path == ledger_path and not changed_once:
            changed_once = True
            ledger_path.write_bytes(replacement)
        return raw

    monkeypatch.setattr(Path, "read_bytes", read_then_change)
    checkpoint_before = checkpoint_path.read_bytes()
    with pytest.raises(ValueError, match="ledger changed during scoreboard projection"):
        ledger.write_checkpoint_scoreboard(root)
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert ledger_path.read_bytes() == replacement
    assert not list(checkpoint_path.parent.glob(".acceptance-ledger-*.tmp"))


def test_checkpoint_checker_reports_nested_malformed_fields_without_crashing(tmp_path):
    root = prepare_root(tmp_path)
    write_fixture_ledger(root)
    checkpoint = make_checkpoint()
    checkpoint["routing_state"] = []
    checkpoint["tests"][0]["result"] = {"unexpected": "object"}
    write_fixture_checkpoint(root, checkpoint)
    errors = ledger.check_checkpoint_scoreboard(root)
    assert errors
    assert any("malformed value types" in error for error in errors)


def test_cli_checkpoint_check_reports_nested_malformed_fields_without_traceback(tmp_path, monkeypatch, capsys):
    root = prepare_root(tmp_path)
    write_fixture_ledger(root)
    checkpoint = make_checkpoint()
    checkpoint["routing_state"] = {"unexpected": "object"}
    write_fixture_checkpoint(root, checkpoint)
    monkeypatch.chdir(root)
    assert ledger.main(["--check-checkpoint"]) == 1
    output = capsys.readouterr().out
    assert "malformed value types" in output
    assert "Traceback" not in output


def test_validation_never_executes_evidence_commands(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    data = make_ledger(root)
    data["steps"][0]["verification"]["command"] = "raise RuntimeError('must stay data')"
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("validator executed supplied command")

    monkeypatch.setattr("subprocess.run", forbidden)
    report = ledger.validate_ledger(data, root)
    assert not called
    assert report.errors == []


def test_module_cli_prints_compact_report_and_check_returns_failure(tmp_path, monkeypatch, capsys):
    root = prepare_root(tmp_path)
    path = root / "planning/acceptance/MJ-REQ-292.json"
    path.parent.mkdir(parents=True)
    data = make_ledger(root)
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.chdir(root)
    assert ledger.main(["--check"]) == 0
    assert capsys.readouterr().out.startswith("MJ-REQ-292: 0/60 recorded PASS claims; structural validation only")

    data["scoreboard"]["NOT_RUN"] = 59
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ledger.main(["--check"]) == 1
    assert "ERROR: scoreboard.NOT_RUN" in capsys.readouterr().out

    data["scoreboard"]["NOT_RUN"] = 60
    path.write_text(json.dumps(data), encoding="utf-8")
    write_fixture_checkpoint(root)
    assert ledger.main(["--write-checkpoint-scoreboard"]) == 0
    assert "updated from structural ledger projection" in capsys.readouterr().out
    assert ledger.main(["--check-checkpoint"]) == 0
    assert "matches ledger bytes" in capsys.readouterr().out


def test_cli_reports_malformed_checkpoint_type_without_traceback(tmp_path, monkeypatch, capsys):
    root = prepare_root(tmp_path)
    path = root / ledger.LEDGER_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(make_ledger(root)), encoding="utf-8")
    checkpoint_path = write_fixture_checkpoint(root)
    checkpoint_path.write_text("[]", encoding="utf-8")
    monkeypatch.chdir(root)
    assert ledger.main(["--write-checkpoint-scoreboard"]) == 1
    output = capsys.readouterr().out
    assert "ERROR: checkpoint must be an object" in output
    assert "Traceback" not in output

