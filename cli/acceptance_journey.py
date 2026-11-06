"""Fail-closed coordinator for the connected MJ-REQ-292 acceptance journey.

Run from a clean repository against an already provisioned, owned fixture stack.
This does not provision a stack, enable a real provider, or edit acceptance ledgers.
Only explicit numbered observations emitted by the executed producers earn credit;
legacy action strings and nearby test results remain supplemental evidence.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit

import httpx

from cli import compose_recovery as recovery
from cli import ui_e2e
from fixtures.company_monitoring_stack import source_files


REQUIREMENT = "MJ-REQ-292"
ROOT = Path(__file__).resolve().parents[1]
EMPTY_HASH = hashlib.sha256(b"").hexdigest()
STATUSES = {"PASS", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED"}
IDENTIFIERS = ("profile_id", "application_id", "operation_id", "request_id")
SOURCE_ROOTS = ("apps", "cli", "fixtures", "infra", "migrations", "packages",
                "planning", "services", "tests", "tools", "workers")
SOURCE_FILES = ("requirements.txt", "requirements.lock", "pyproject.toml")
EXCLUDED_PARTS = {"node_modules", "__pycache__", ".pytest_cache", "dist"}
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
# Fixed assertion identifiers agreed with the connected UI producer. This is a
# reviewed-code test contract, not an independent verification certification.
# No 36-60 emitter is currently registered; in particular, running scheduler
# processes cannot stand in for naturally due monitoring observations (59-60).
REQUIRED_CHECKS = {
    2: {"dashboard_heading_visible"},
    3: {"new_profile_saved_and_selected"},
    4: {"resume_upload_and_extraction_visible"},
    5: {"extracted_suggestions_remain_unapproved"},
    6: {"corrected_fact_read_back", "corrected_fact_remains_pending"},
    7: {"approved_fact_set_matches_review"},
    8: {"remote_it_search_read_back", "remote_data_search_read_back"},
    9: {"source_rows_visible", "multiple_sources_enabled"},
    10: {"discovery_operation_completed", "search_scoped_job_readback"},
    12: {"matching_job_visible", "nonmatching_job_hidden"},
    13: {"filtered_job_visible", "other_title_hidden"},
    14: {"match_sort_order"},
    15: {"salary_range_visible", "salary_filter_retains_jobs"},
    16: {"remote_filter_returned_a_remote_job", "remote_job_type_visible", "employer_remote_restrictions_available"},
    17: {"canonical_duplicate_job_available", "duplicate_count_matches_readback"},
    18: {"canonical_job_detail_visible"},
    19: {"source_copy_ids_read_back", "source_copy_panels_visible"},
    20: {"verified_ats_authority_visible", "preferred_route_readback"},
    21: {"application_route_visible"},
    22: {"match_score_explained"},
    32: {"strong_match_available", "saved_status_readback"},
    33: {"tailored_resume_uses_selected_approved_facts", "omitted_fact_visible_and_excluded"},
    34: {"generated_base_visible", "tailored_version_visible", "omission_and_change_summary_visible"},
    35: {"cover_letter_generated", "cross_job_uniqueness_compared"},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_clean_source(source):
    if (not isinstance(source, dict) or source.get("state") is not None
            or not isinstance(source.get("commit"), str) or not SHA40.fullmatch(source["commit"])
            or not isinstance(source.get("tree"), str) or not SHA40.fullmatch(source["tree"])
            or source.get("working_tree_diff_sha256") != EMPTY_HASH
            or source.get("working_tree_status_sha256") != EMPTY_HASH):
        raise ValueError("Journey requires exact clean tracked and untracked source binding")
    return {key: source[key] for key in (
        "commit", "tree", "working_tree_diff_sha256", "working_tree_status_sha256")}


def current_source():
    if Path.cwd().resolve() != ROOT.resolve():
        raise ValueError("Run the journey from its repository root")
    return validate_clean_source(ui_e2e._source_binding())


def validate_target(base_url, project_name, port):
    recovery.validate_project_name(project_name)
    recovery.validate_port(port)
    target = urlsplit(base_url)
    if (target.scheme != "http" or target.hostname != "127.0.0.1"
            or target.port != port or target.username or target.password
            or target.path not in ("", "/") or target.query or target.fragment):
        raise ValueError("Journey requires the exact loopback fixture origin and port")
    return {"base_url": f"http://127.0.0.1:{port}", "project_name": project_name, "port": port}


def _source_member(name):
    parts = Path(name).parts
    return bool(parts and (parts[0] in SOURCE_ROOTS or name in SOURCE_FILES)
                and not EXCLUDED_PARTS.intersection(parts))


@lru_cache(maxsize=2)
def source_archive_sha256(commit):
    """Hash the deterministic Git archive used by the operator's actual build."""
    with tempfile.TemporaryDirectory(prefix="mjobs-journey-source-") as directory:
        archive = Path(directory) / "source.tar"
        subprocess.run(["git", "archive", "--format=tar", "--output", str(archive), commit],
                       cwd=ROOT, check=True, timeout=90, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hashed = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hashed.update(chunk)
        return hashed.hexdigest()


def _validate_asset_manifest(assets):
    if not isinstance(assets, dict) or "index.html" not in assets or not 1 <= len(assets) <= 1000:
        raise ValueError("Invalid frontend asset manifest")
    for name, sha in assets.items():
        if (not isinstance(name, str) or len(name) > 240 or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
                or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))
                or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)):
            raise ValueError("Invalid frontend asset manifest member")
    return assets


def verify_served_assets(base_url, assets):
    """Hash actual static responses from the already-guarded owned loopback API."""
    ui_e2e.validate_onboarding_target(base_url)
    _validate_asset_manifest(assets)
    total = 0
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        for name, expected in [("", assets["index.html"]), *sorted(assets.items())]:
            if time.monotonic() >= deadline:
                raise TimeoutError("Served frontend asset verification deadline exceeded")
            hashed = hashlib.sha256()
            size = 0
            with client.stream("GET", base_url + "/" + quote(name, safe="/")) as response:
                if response.status_code != 200:
                    raise ValueError("Owned API did not serve the expected frontend asset")
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Served frontend asset verification deadline exceeded")
                    size += len(chunk)
                    total += len(chunk)
                    if size > 16 * 1024 * 1024 or total > 64 * 1024 * 1024:
                        raise ValueError("Served frontend asset byte bound exceeded")
                    hashed.update(chunk)
            if hashed.hexdigest() != expected:
                raise ValueError("Served frontend asset differs from the runtime image")


def validate_frontend_build_receipt(receipt, source, runtime):
    """Validate an operator-owned actual-build receipt, never execute its command.

    The trust boundary is reviewed exact-source producers and owned local build
    artifacts. This is not a signature against a malicious local disk owner.
    """
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("result") != "PASS"
            or receipt.get("source_commit") != source["commit"]
            or receipt.get("source_tree") != source["tree"]
            or receipt.get("source_archive_sha256") != source_archive_sha256(source["commit"])
            or receipt.get("dockerfile_sha256") != runtime["dockerfile_sha256"]
            or receipt.get("image_id") != runtime["images"]["api"]
            or receipt.get("frontend_assets") != runtime["frontend_assets"]):
        raise ValueError("Owned frontend build receipt source, image or asset binding mismatch")
    _validate_asset_manifest(receipt["frontend_assets"])
    command = receipt.get("build_command")
    if (not isinstance(command, list) or not 1 <= len(command) <= 40
            or any(not isinstance(part, str) or not part or len(part) > 500 for part in command)):
        raise ValueError("Owned frontend build receipt lacks its executed build command")
    try:
        completed = datetime.fromisoformat(receipt["completed_at_utc"])
        if completed.tzinfo is None or completed > datetime.now(timezone.utc) + timedelta(seconds=5):
            raise ValueError
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("Owned frontend build receipt timestamp is invalid") from error
    return {"status": "PASS", "receipt_sha256": digest(receipt), "receipt": copy.deepcopy(receipt)}


def preflight(target, source):
    """Read-only existing ownership guards plus actual container source/image proof.

    Source hashes are Git-exported bytes. An image copied from a checkout with
    different line endings fails this strict check; build from committed bytes.
    Frontend bundle build provenance remains a separate reviewed build receipt.
    """
    configs = recovery.resolve_compose_files()
    targets = recovery.preflight_recovery_targets(
        target["project_name"], target["port"], config_files=configs)
    expected = {name: value for name, value in source_files(ROOT, source["commit"]).items()
                if _source_member(name)}
    if not expected or "cli/ui_e2e.py" not in expected:
        raise ValueError("Committed runtime source manifest is incomplete")
    # Fixed code, fixed roots, hashes only: no environment or source text is emitted.
    program = (
        "import hashlib,json,pathlib; root=pathlib.Path.cwd(); "
        f"roots={SOURCE_ROOTS!r}; singles={SOURCE_FILES!r}; excluded={sorted(EXCLUDED_PARTS)!r}; "
        "paths=[p for name in roots for p in (root/name).rglob('*') if p.is_file()]"
        "+[root/name for name in singles if (root/name).is_file()]; "
        "paths=[p for p in paths if not set(p.relative_to(root).parts).intersection(excluded)]; "
        "assert not any(p.is_symlink() for p in paths); "
        "print(json.dumps({p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() "
        "for p in paths},sort_keys=True))"
    )
    images = {}
    containers = {}
    frontend_assets = None
    for service in recovery.RECOVERY_RESTART_SERVICES:
        container_id = targets[service]["container_id"]
        # Ownership was verified above; the inspection projection cannot leak Env.
        detail = json.loads(recovery._run_bounded(
            ["docker", "inspect", "--format", '{{json .Image}}', container_id],
            timeout=10, action="Inspect journey image"))
        if not isinstance(detail, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", detail):
            raise ValueError("Runtime image identity is unavailable")
        images[service] = detail
        containers[service] = container_id
        if service not in {"postgres", "redis", "mock"}:
            # Emit only two safe literal matches, never environment credentials.
            mode = recovery._run_bounded([
                "docker", "inspect", "--format",
                '{{range .Config.Env}}{{if or (eq . "MJOBS_OPERATION_MODE=FIXTURE") '
                '(eq . "MJOBS_COMPOSE_MOCK=1")}}{{println .}}{{end}}{{end}}', container_id],
                timeout=10, action="Inspect journey fixture mode")
            if set(mode.splitlines()) != {"MJOBS_OPERATION_MODE=FIXTURE", "MJOBS_COMPOSE_MOCK=1"}:
                raise ValueError("Every journey runtime worker must remain in fixture mode")
        if service not in {"postgres", "redis"}:
            actual = json.loads(recovery._run_bounded(
                ["docker", "exec", container_id, "python", "-c", program],
                timeout=90, action="Verify committed journey runtime source"))
            if actual != expected:
                raise ValueError("Runtime source bytes differ from the exact committed source")
            if service == "api":
                asset_program = (
                    "import hashlib,json,pathlib; root=pathlib.Path('apps/web/dist'); "
                    "paths=[p for p in root.rglob('*') if p.is_file()]; "
                    "assert not any(p.is_symlink() for p in paths); "
                    "print(json.dumps({p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() "
                    "for p in paths},sort_keys=True))")
                frontend_assets = _validate_asset_manifest(json.loads(recovery._run_bounded(
                    ["docker", "exec", container_id, "python", "-c", asset_program],
                    timeout=30, action="Hash journey frontend assets")))
    # Repeat the existing read-only live API gate for resume/recovery as well as
    # initial onboarding; a previously safe baseline cannot authorize new modes.
    ui_e2e._onboarding_preflight(target["base_url"])
    verify_served_assets(target["base_url"], frontend_assets)
    return {"source_manifest_sha256": digest(expected), "images": images,
            "containers": containers, "compose_files": [str(p) for p in configs],
            "frontend_assets": frontend_assets, "dockerfile_sha256": expected["infra/docker/Dockerfile"]}


def _runtime_identity(binding):
    # Compose can replace a worker during recovery; immutable images and source
    # must remain the same, while container IDs are recorded per phase.
    return {key: binding[key] for key in (
        "source_manifest_sha256", "images", "compose_files", "frontend_assets", "dockerfile_sha256")}


def _read_artifact(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("Missing, unsafe or oversized producer artifact")
    encoded = path.read_bytes()
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("Producer artifact must be an object")
    return value, hashlib.sha256(encoded).hexdigest()


def _identity(baseline):
    recovery.validate_baseline(baseline)
    return {key: baseline[key] for key in IDENTIFIERS}


def numbered_observations(artifact, *, segment, run_id, source, identity):
    """Validate explicit producer assertions, never infer numbered checks from prose.

    Artifact paths/hashes are added by the coordinator after producer completion,
    avoiding a self-referential hash. PUBLIC_READ_ONLY observations are deliberately
    not admitted by this fixture-only coordinator; a separate approved public
    collection segment and provenance validator are required for criterion 11.
    """
    rows = artifact.get("acceptance_observations", [])
    if not isinstance(rows, list) or len(rows) > 60:
        raise ValueError("Invalid numbered observation collection")
    if not rows:
        return []
    if artifact.get("acceptance_run_id") != run_id or artifact.get("source") != source:
        raise ValueError("Observation artifact run or source binding mismatch")
    seen = set()
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid numbered observation")
        step = row.get("step")
        if (type(step) is not int or not 1 <= step <= 60 or step in seen
                or (segment == "ui" and step >= 54)
                or (segment == "recovery" and step < 54)):
            raise ValueError("Duplicate or out-of-segment numbered observation")
        seen.add(step)
        if row.get("run_id") != run_id or row.get("source_commit") != source["commit"]:
            raise ValueError("Numbered observation run or source mismatch")
        status = row.get("status")
        if status not in STATUSES or row.get("evidence_kind") != "CONTROLLED_FIXTURE":
            raise ValueError("Unsupported observation status or source semantics")
        observable = row.get("observable_result")
        if not isinstance(observable, str) or not observable.strip() or len(observable) > 500:
            raise ValueError("Numbered observation requires bounded sanitized observable result")
        continuity = row.get("continuity")
        if not isinstance(continuity, dict) or not set(continuity).issubset(IDENTIFIERS):
            raise ValueError("Invalid continuity identifiers")
        for key, value in continuity.items():
            if (not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                    or identity.get(key) != value):
                raise ValueError("Numbered observation continuity identity mismatch")
        if status == "PASS":
            required = set(IDENTIFIERS if step >= 54 else
                           ("profile_id", "application_id") if step >= 36 else
                           ("profile_id",) if step >= 4 else ())
            if not required.issubset(continuity):
                raise ValueError("PASS observation is missing continuity identity")
            if step == 11:
                raise ValueError("Criterion 11 cannot PASS from fixture evidence")
        checks = row.get("checks")
        if (not isinstance(checks, list) or len(checks) > 30
                or any(not isinstance(check, dict)
                       or not isinstance(check.get("name"), str)
                       or not SAFE_ID.fullmatch(check["name"])
                       or type(check.get("passed")) is not bool for check in checks)):
            raise ValueError("Numbered observations require bounded named boolean checks")
        if status == "PASS" and (not checks or not all(check["passed"] for check in checks)):
            raise ValueError("PASS requires positive executed numbered checks")
        if step == 55 and status == "PASS":
            if artifact.get("public_job_inventory_recovery", {}).get("result") != "PASS":
                raise ValueError("Criterion 55 requires positive complete inventory recovery")
        if status == "PASS":
            names = [check["name"] for check in checks]
            if (step not in REQUIRED_CHECKS or set(names) != REQUIRED_CHECKS[step]
                    or len(names) != len(set(names))):
                raise ValueError("PASS requires exactly the registered step-specific checks")
        result.append({"step": step, "status": status, "observable_result": observable,
                       "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": source["commit"],
                       "run_id": run_id, "continuity": dict(continuity), "checks": copy.deepcopy(checks)})
    return result


class JourneyCheckpoint(ui_e2e.EvidenceJournal):
    """Reuse the existing fsync + replace journal for bounded resumable state."""

    @classmethod
    def create(cls, path, run_id, source, target):
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ValueError("Invalid journey run identity")
        return cls(path, {"schema_version": 1, "requirement_id": REQUIREMENT,
                         "run_id": run_id, "source": validate_clean_source(source),
                         "target": target, "started_at_utc": utc_now(), "result": "PARTIAL",
                         "review_status": "NOT_RUN", "frontend_build": {"status": "NOT_RUN"},
                         "trust_boundary": "Reviewed exact-source producers and operator-owned local artifacts; observed PASS is not independent review or ledger acceptance.",
                         "segments": {name: {"status": "NOT_RUN"} for name in ("ui", "recovery")},
                         "steps": [{"number": number, "status": "NOT_RUN"} for number in range(1, 61)],
                         "limitations": [
                             "No public-source criterion 11 acceptance from controlled fixtures.",
                             "Only explicit numbered producer observations receive credit.",
                             "Frontend bundle build provenance requires a separate build receipt."]})

    @classmethod
    def resume(cls, path, run_id, source, target):
        saved, _ = _read_artifact(path)
        if (saved.get("schema_version") != 1 or saved.get("requirement_id") != REQUIREMENT
                or saved.get("run_id") != run_id or saved.get("source") != validate_clean_source(source)
                or saved.get("target") != target):
            raise ValueError("Checkpoint run, source or target identity mismatch")
        segments = saved.get("segments")
        if not isinstance(segments, dict) or set(segments) != {"ui", "recovery"}:
            raise ValueError("Invalid checkpoint segments")
        if any(segment.get("status") not in {"COMPLETE", "NOT_RUN"} for segment in segments.values()):
            raise ValueError("Cannot resume an interrupted or failed segment; reconcile and start a new run")
        restored = cls.create(path, run_id, source, target)
        restored.binding["started_at_utc"] = saved["started_at_utc"]
        restored.binding["runtime"] = saved.get("runtime")
        restored.binding["runtime_checks"] = saved.get("runtime_checks", {})
        restored.binding["frontend_build"] = saved.get("frontend_build", {"status": "NOT_RUN"})
        restored.binding["resume_imported"] = True
        for name in ("ui", "recovery"):
            recorded = segments[name]
            if recorded["status"] == "COMPLETE":
                artifact_path = Path(path).parent / f"{name}.json"
                if recorded.get("artifact") != artifact_path.name:
                    raise ValueError("Checkpoint artifact path mismatch")
                artifact, sha = _read_artifact(artifact_path)
                if recorded.get("sha256") != sha:
                    raise ValueError("Checkpoint artifact hash mismatch")
                restored.complete_segment(name, artifact, sha, persist=False)
                for row in restored.binding["steps"]:
                    if row.get("verification", {}).get("evidence_artifact") == f"{name}.json":
                        row["verification"]["resume_imported"] = True
        return restored

    def payload(self):
        return copy.deepcopy(self.binding)

    def begin(self, name):
        if name not in self.binding["segments"] or self.binding["segments"][name]["status"] != "NOT_RUN":
            raise ValueError("Segment cannot be replayed")
        if name == "recovery" and self.binding["segments"]["ui"]["status"] != "COMPLETE":
            raise ValueError("Recovery requires a completed connected UI baseline")
        self.binding["active_segment"] = name
        self.binding["segments"][name] = {"status": "IN_PROGRESS", "started_at_utc": utc_now()}
        self.persist()

    def complete_segment(self, name, artifact, sha, *, persist=True):
        try:
            timestamp = datetime.fromisoformat(artifact["timestamp"])
            started = datetime.fromisoformat(self.binding["started_at_utc"])
            if (timestamp.tzinfo is None or started.tzinfo is None or timestamp < started
                    or timestamp > datetime.now(timezone.utc) + timedelta(seconds=5)):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Producer artifact timestamp is missing, invalid or stale") from error
        if name == "ui":
            if artifact.get("source") != self.binding["source"]:
                raise ValueError("Connected UI source binding mismatch")
            if artifact.get("target_origin") != self.binding["target"]["base_url"]:
                raise ValueError("Connected UI origin binding mismatch")
            if artifact.get("mode") != "ONBOARDING_FIXTURE" or artifact.get("result") not in {"PASS", "PARTIAL"}:
                raise ValueError("Connected UI baseline is incomplete")
            identity = _identity(artifact)
            self.binding["continuity"] = identity
        else:
            identity = self.binding.get("continuity")
            if not identity or artifact.get("application_id") != identity["application_id"]:
                raise ValueError("Recovery application identity mismatch")
            if artifact.get("result") != "PASS" or artifact.get("external_submission_enabled") is not False:
                raise ValueError("Recovery did not complete safely")
            compose_target = artifact.get("compose_target", {})
            if (compose_target.get("project_name") != self.binding["target"]["project_name"]
                    or compose_target.get("port") != self.binding["target"]["port"]
                    or compose_target.get("profile") != "fixture"
                    or compose_target.get("project_directory") != str(ROOT.resolve())
                    or compose_target.get("compose_files") != self.binding["runtime"]["compose_files"]):
                raise ValueError("Recovery target identity mismatch")
            if any(row.get("step") == 55 and row.get("status") == "PASS"
                   for row in artifact.get("acceptance_observations", [])):
                baseline, _ = _read_artifact(self.path.parent / "ui.json")
                inventory = baseline.get("job_inventory")
                recovery.validate_public_job_inventory(inventory)
                if inventory.get("schema_version") != recovery.JOB_INVENTORY_VERSION:
                    raise ValueError("Criterion 55 requires current complete baseline inventory")
                recovered = artifact.get("public_job_inventory_recovery", {})
                if any(recovered.get(key) != inventory[key]
                       for key in ("profile_id", "sha256", "job_count", "source_copy_count")):
                    raise ValueError("Criterion 55 inventory differs from the connected UI baseline")
        rows = numbered_observations(artifact, segment=name, run_id=self.binding["run_id"],
                                    source=self.binding["source"], identity=identity)
        for row in rows:
            report_row = {
                "number": row["step"], "status": row["status"], "observation": row,
                "verification": {"tested_commit": self.binding["source"]["commit"],
                                 "evidence_artifact": f"{name}.json", "evidence_sha256": sha,
                                 "run_id": self.binding["run_id"], "evidence_kind": row["evidence_kind"],
                                 "command": "python -m cli.ui_e2e --onboarding" if name == "ui" else
                                            "python -m cli.compose_recovery --baseline ui.json",
                                 "observed_result": row["observable_result"],
                                 "executed_at_utc": artifact["timestamp"],
                                 "continuity": row["continuity"]}}
            if name == "ui" and row["status"] == "PASS" and self.binding["frontend_build"]["status"] != "PASS":
                report_row.update(status="PARTIAL", producer_status="PASS", credit_withheld="FRONTEND_BUILD_NOT_BOUND")
            self.binding["steps"][row["step"] - 1] = report_row
        self.binding["segments"][name] = {"status": "COMPLETE", "artifact": f"{name}.json", "sha256": sha}
        self.binding.pop("active_segment", None)
        # All 60 must actually be observed. A fixture-only run cannot satisfy 11.
        statuses = [row["status"] for row in self.binding["steps"]]
        self.binding["result"] = "FAIL" if "FAIL" in statuses else "PASS" if set(statuses) == {"PASS"} else "PARTIAL"
        self.binding["scoreboard"] = {"observed_PASS" if status == "PASS" else status: statuses.count(status)
                                      for status in sorted(STATUSES)}
        self.binding["updated_at_utc"] = utc_now()
        if persist:
            self.persist()

    def fail(self, error):
        name = self.binding.get("active_segment")
        if name:
            self.binding["segments"][name]["status"] = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "FAIL"
        self.binding["result"] = "FAIL"
        # Exception messages can contain PII, tokens or raw response payloads.
        self.binding["failure"] = {"category": "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "SEGMENT_FAILED"}
        self.persist()


def _invoke(producer, args, run_id, **kwargs):
    # Older producers are executable but have no numbered acceptance credit.
    if "acceptance_run_id" in inspect.signature(producer).parameters:
        kwargs["acceptance_run_id"] = run_id
    producer(*args, **kwargs)


def run(*, base_url, evidence_dir, project_name, port, run_id, resume=False, stop_after_ui=False,
        build_receipt=None):
    target = validate_target(base_url, project_name, port)
    source = current_source()
    directory = Path(evidence_dir).resolve()
    # Never overwrite an earlier run or put private runtime artifacts in tracked source.
    if directory == ROOT or ROOT in directory.parents and not (ROOT / "outputs") in directory.parents:
        raise ValueError("Journey artifacts must be outside source or inside ignored outputs")
    path = directory / "journey.json"
    if not resume and directory.exists() and any(directory.iterdir()):
        raise ValueError("New journey requires an empty evidence directory")
    checkpoint = (JourneyCheckpoint.resume(path, run_id, source, target) if resume
                  else JourneyCheckpoint.create(path, run_id, source, target))
    runtime = preflight(target, source)
    previous = checkpoint.binding.get("runtime")
    if previous is not None and _runtime_identity(previous) != _runtime_identity(runtime):
        raise ValueError("Runtime source or image identity changed since checkpoint")
    checkpoint.binding["runtime"] = runtime
    if build_receipt is not None:
        receipt, _ = _read_artifact(build_receipt)
        checkpoint.binding["frontend_build"] = validate_frontend_build_receipt(receipt, source, runtime)
    elif checkpoint.binding["frontend_build"]["status"] == "PASS":
        validate_frontend_build_receipt(checkpoint.binding["frontend_build"]["receipt"], source, runtime)
    checkpoint.persist()
    try:
        for name in ("ui", "recovery"):
            if name == "recovery" and stop_after_ui:
                break
            if checkpoint.binding["segments"][name]["status"] == "COMPLETE":
                continue
            artifact_path = directory / f"{name}.json"
            if artifact_path.exists():
                raise ValueError("Refusing to reuse an uncheckpointed producer artifact")
            if current_source() != source:
                raise ValueError("Source changed during journey")
            latest = preflight(target, source)
            if _runtime_identity(latest) != _runtime_identity(runtime):
                raise ValueError("Runtime source or images changed during journey")
            checkpoint.begin(name)
            if name == "ui":
                _invoke(ui_e2e.run, (target["base_url"], artifact_path), run_id, onboarding=True)
            else:
                # This is exactly the checkpointed connected UI artifact, not a
                # nearby test receipt or a baseline from another profile/run.
                baseline, sha = _read_artifact(directory / "ui.json")
                if sha != checkpoint.binding["segments"]["ui"]["sha256"]:
                    raise ValueError("UI baseline changed before recovery")
                _identity(baseline)
                _invoke(recovery.run, (directory / "ui.json", artifact_path, project_name, port), run_id)
            artifact, sha = _read_artifact(artifact_path)
            if current_source() != source:
                raise ValueError("Source changed while producer was executing")
            completed_runtime = preflight(target, source)
            if _runtime_identity(completed_runtime) != _runtime_identity(runtime):
                raise ValueError("Runtime source or image changed while producer was executing")
            checkpoint.binding.setdefault("runtime_checks", {})[name] = {
                "before": latest, "after": completed_runtime}
            checkpoint.complete_segment(name, artifact, sha)
        return checkpoint.payload()
    except BaseException as error:
        try:
            checkpoint.fail(error)
        except Exception:
            pass  # Preserve the primary failure, as EvidenceJournal's caller does.
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-ui", action="store_true")
    parser.add_argument("--build-receipt", help="Owned actual-build receipt; absent receipt withholds UI observed PASS")
    args = parser.parse_args(argv)
    report = run(**vars(args))
    print(json.dumps({"result": report["result"], "run_id": report["run_id"],
                      "scoreboard": report.get("scoreboard", {})}, sort_keys=True))
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
