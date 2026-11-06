"""Fail-closed coordinator for the connected MJ-REQ-292 acceptance journey.

Run from a clean repository against an already provisioned, owned fixture stack.
This does not provision a stack or edit acceptance ledgers. The default CLI never
contacts providers; public collection requires explicit programmatic opt-in and
a root-owned containment callback.
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
from cli import local_persistence_acceptance as persistence
from cli import ui_e2e
from fixtures.company_monitoring_stack import source_files


REQUIREMENT = "MJ-REQ-292"
ROOT = Path(__file__).resolve().parents[1]
EMPTY_HASH = hashlib.sha256(b"").hexdigest()
STATUSES = {"PASS", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED"}
IDENTIFIERS = ("profile_id", "application_id", "operation_id", "request_id")
CONTINUITY_KEYS = frozenset((*IDENTIFIERS, "job_id", "document_id", "document_ids"))
SOURCE_ROOTS = ("apps", "cli", "fixtures", "infra", "migrations", "packages",
                "planning", "services", "tests", "tools", "workers")
SOURCE_FILES = ("requirements.txt", "requirements.lock", "pyproject.toml")
EXCLUDED_PARTS = {"node_modules", "__pycache__", ".pytest_cache", "dist"}
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
# Fixed assertion identifiers agreed with the connected UI producer. This is a
# reviewed-code test contract, not an independent verification certification.
# Running scheduler processes cannot stand in for naturally due observations.
REQUIRED_CHECKS = {
    1: {"fresh_owned_stack_created", "startup_source_run_target_bound", "startup_images_and_containers_bound",
        "startup_all_services_ready", "startup_migration_completed"},
    2: {"dashboard_heading_visible"},
    3: {"first_run_wizard_completed_with_persisted_configuration"},
    4: {"resume_upload_and_extraction_visible"},
    5: {"extracted_suggestions_remain_unapproved"},
    6: {"corrected_fact_read_back", "corrected_fact_remains_pending"},
    7: {"approved_fact_set_matches_review"},
    8: {"remote_it_search_read_back", "remote_data_search_read_back"},
    9: {"source_rows_visible", "multiple_sources_enabled"},
    10: {"discovery_operation_completed", "search_scoped_job_readback"},
    11: {"same_profile_public_readonly_jobs_observed", "public_jobs_visible_in_same_profile_ui"},
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
    22: {"visible_numeric_match_score", "selected_job_requirement_explanation_visible"},
    23: {"previously_unknown_employer_observed"},
    24: {"employer_discovery_ran_automatically"},
    25: {"official_employer_domain_verified"},
    26: {"employer_careers_page_found"},
    27: {"employer_ats_tenant_identified"},
    28: {"employer_source_enrolled_automatically"},
    29: {"employer_openings_enumerated"},
    30: {"employer_only_opening_observed"},
    31: {"employer_duplicate_sources_collapsed"},
    32: {"strong_match_available", "saved_status_readback"},
    33: {"tailored_resume_uses_selected_approved_facts", "omitted_fact_visible_and_excluded"},
    34: {"generated_base_visible", "tailored_version_visible", "omission_and_change_summary_visible"},
    35: {"cover_letter_generated", "cross_job_uniqueness_compared"},
    36: {"application_prepared_readback", "profile_job_pins_match"},
    37: {"selected_route_matches_application", "route_bound_to_job"},
    38: {"supported_adapter_matches_provider", "provider_matches_route"},
    39: {"playwright_form_opened", "selected_application_loaded"},
    40: {"contact_fields_match_verified_profile", "contact_fields_readback_verified"},
    41: {"resume_upload_matches_document", "resume_sha256_matches"},
    42: {"cover_letter_upload_matches_document", "cover_letter_sha256_matches"},
    43: {"approved_answers_match_questions", "approved_answers_readback_verified"},
    44: {"uncertain_question_paused", "submit_not_attempted"},
    45: {"explicit_answer_approval_persisted", "approval_bound_to_question"},
    46: {"reviewed_application_resumed", "stale_approval_rejected"},
    47: {"current_form_validated", "profile_document_pins_match"},
    48: {"dry_run_receipt_persisted", "no_external_post_recorded"},
    49: {"owned_mock_received_submit", "external_submission_absent"},
    50: {"authoritative_confirmation_detected", "confirmation_count_one"},
    51: {"history_receipt_persisted", "repeated_readback_idempotent"},
    52: {"tracker_matches_application", "tracker_survives_reload"},
    53: {"audit_sequence_complete", "audit_scope_matches_application", "audit_values_redacted"},
    54: {"all_recovery_services_restarted", "recovered_services_healthy"},
    55: {"same_profile_v3_inventory_preserved"},
    56: {"company_source_catalog_preserved"},
    57: {"uploaded_resumes_preserved", "generated_resumes_preserved", "cover_letters_preserved"},
    58: {"application_history_preserved", "same_confirmed_mock_receipt_recovered"},
    59: {"restart_scheduler_natural_operation_observed", "scheduler_admitted_at_persisted_due",
         "scheduler_same_profile_source", "scheduler_no_manual_enqueue", "scheduler_single_operation"},
    60: {"natural_due_time_reached", "same_profile_direct_source_refresh_completed",
         "refresh_attempt_count_one", "direct_source_identity_preserved", "refresh_receipt_pointer_advanced",
         "next_due_advanced", "no_duplicate_refresh", "stable_seven_domains_and_v3_inventory"},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _instant(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid aware evidence timestamp") from error


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
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt.get("schema_version") != 1
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
    service_started_at = {}
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
        started = recovery._run_bounded(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container_id],
            timeout=10, action="Inspect journey service start").strip()
        if not datetime(1970, 1, 1, tzinfo=timezone.utc) < _instant(started) <= datetime.now(timezone.utc) + timedelta(seconds=5):
            raise ValueError("Invalid running service start timestamp")
        service_started_at[service] = started
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
            "frontend_assets": frontend_assets, "dockerfile_sha256": expected["infra/docker/Dockerfile"],
            "service_started_at": service_started_at}


def _runtime_identity(binding):
    # Compose can replace a worker during recovery; immutable images and source
    # must remain the same, while container IDs are recorded per phase.
    return {key: binding[key] for key in (
        "source_manifest_sha256", "images", "compose_files", "frontend_assets", "dockerfile_sha256")}


def observe_startup_runtime(target, runtime):
    """Read only the initially admitted project, including its exited migration."""
    observed = copy.deepcopy(runtime)
    observed["container_created_at"] = {}
    for service, container_id in runtime["containers"].items():
        values = recovery._run_bounded(
            ["docker", "inspect", "--format", "{{.Created}}|{{.State.StartedAt}}", container_id],
            timeout=10, action="Inspect initial journey container creation").strip().split("|")
        if len(values) != 2:
            raise ValueError("Startup creation inspection malformed")
        observed["container_created_at"][service], observed["service_started_at"][service] = values
    configs = tuple(Path(value) for value in runtime["compose_files"])
    candidates = recovery.compose(target["project_name"], target["port"], "ps", "--all", "-q", "migrate", config_files=configs).split()
    if len(candidates) != 1:
        raise ValueError("Startup migration target is missing or ambiguous")
    migration_id = candidates[0]
    recovery.inspect_container(target["project_name"], "migrate", migration_id, config_files=configs, require_running=False)
    data = recovery._run_bounded(["docker", "inspect", "--format",
        "{{.State.Status}}|{{.State.ExitCode}}|{{.State.FinishedAt}}|{{.Created}}|{{.Image}}", migration_id],
        timeout=10, action="Inspect initial journey migration").strip().split("|")
    if len(data) != 5:
        raise ValueError("Startup migration inspection malformed")
    observed["migration"] = {"container_id": migration_id, "status": data[0], "exit_code": int(data[1]),
        "finished_at_utc": data[2], "created_at": data[3], "image_id": data[4]}
    observed["health"] = recovery.healthy_containers({name: {"container_id": value} for name, value in runtime["containers"].items()})
    return observed


def validate_startup_receipt(receipt, source, run_id, target, runtime):
    """Admit the operator's initial command receipt against fresh owned readbacks.

    Empty preexisting-container evidence is an operator-observed historical fact;
    this does not claim authentication against a malicious local administrator.
    """
    if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("result") != "PASS" or receipt.get("source") != source
            or receipt.get("acceptance_run_id") != run_id or receipt.get("target") != target
            or receipt.get("preexisting_container_ids") != []
            or receipt.get("preexisting_volume_ids") != []
            or type(receipt.get("command_exit_code")) is not int or receipt["command_exit_code"] != 0):
        raise ValueError("Startup receipt source, run, target or initial ownership mismatch")
    command = receipt.get("command")
    if (not isinstance(command, list) or not 6 <= len(command) <= 80
            or any(not isinstance(value, str) or not value or len(value) > 512 for value in command)
            or command[:2] != ["docker", "compose"] or "up" not in command
            or not any(flag in command[command.index("up") + 1:] for flag in ("-d", "--detach"))
            or [command[index + 1] for index, value in enumerate(command[:-1]) if value in {"-p", "--project-name"}] != [target["project_name"]]
            or any(value.startswith("--project-name=") or value.startswith("-p") and value not in {"-p", "--pull"}
                   for value in command)):
        raise ValueError("Startup receipt requires the actual owned detached Compose up command")
    files = [str((ROOT / command[index + 1]).resolve()) for index, value in enumerate(command[:-1]) if value in {"-f", "--file"}]
    if files != [str(Path(value).resolve()) for value in runtime["compose_files"]]:
        raise ValueError("Startup command Compose files differ from the admitted project")
    started, completed = _instant(receipt.get("started_at_utc")), _instant(receipt.get("completed_at_utc"))
    if not started < completed <= datetime.now(timezone.utc) + timedelta(seconds=5):
        raise ValueError("Startup receipt time window is invalid")
    services = set(recovery.RECOVERY_RESTART_SERVICES)
    for key in ("containers", "images", "container_created_at", "service_started_at", "health"):
        if (not isinstance(runtime.get(key), dict) or set(runtime[key]) != services
                or receipt.get(key) != runtime[key]):
            raise ValueError("Startup receipt differs from initial runtime " + key)
    if len(set(runtime["containers"].values())) != len(services):
        raise ValueError("Startup container identities must be distinct")
    for service in services:
        if (not isinstance(runtime["containers"][service], str) or not SAFE_ID.fullmatch(runtime["containers"][service])
                or not isinstance(runtime["images"][service], str)
                or re.fullmatch(r"sha256:[a-f0-9]{64}", runtime["images"][service]) is None
                or runtime["health"][service] != {"status": "running", "health": "healthy"}
                or not started <= _instant(runtime["container_created_at"][service]) <= _instant(runtime["service_started_at"][service]) <= completed):
            raise ValueError("Startup requires freshly created, running and healthy owned services")
    migration = runtime.get("migration", {})
    if (receipt.get("migration") != migration or migration.get("status") != "exited"
            or type(migration.get("exit_code")) is not int or migration["exit_code"] != 0
            or not isinstance(migration.get("container_id"), str) or not SAFE_ID.fullmatch(migration["container_id"])
            or migration["container_id"] in runtime["containers"].values()
            or migration.get("image_id") != runtime["images"]["api"]
            or not started <= _instant(migration.get("created_at")) <= _instant(migration.get("finished_at_utc")) <= completed):
        raise ValueError("Startup requires the same-build owned migration to exit zero")
    return {"step": 1, "status": "PASS", "observable_result": "Recorded initial Compose command created the owned fixture stack; migration exited zero and all ten services are ready",
        "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": source["commit"], "run_id": run_id,
        "continuity": {}, "checks": [{"name": name, "passed": True} for name in sorted(REQUIRED_CHECKS[1])]}


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
    identity = {key: baseline[key] for key in IDENTIFIERS}
    if ("selected_application_id" in baseline
            and baseline["selected_application_id"] != identity["application_id"]):
        raise ValueError("Selected application identity differs from the recovery baseline")
    if "selected_application_job_id" in baseline:
        value = baseline["selected_application_job_id"]
        if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
            raise ValueError("Invalid selected application job identity")
        identity["job_id"] = value
    for field, key, maximum in (("observed_job_ids", "observed_job_ids", 200),
                                 ("application_document_ids", "document_ids", 100)):
        if field in baseline:
            identity[key] = _bounded_identity_ids(baseline[field], maximum, "readback identity")
    if "employer_discovery_lineage" in baseline:
        lineage = baseline["employer_discovery_lineage"]
        if (not isinstance(lineage, dict) or set(lineage) != {"profile_id", "operation_id", "request_id", "job_ids"}
                or lineage.get("profile_id") != identity["profile_id"]
                or any(not isinstance(lineage.get(key), str) or not SAFE_ID.fullmatch(lineage[key])
                       for key in ("operation_id", "request_id"))
                or not set(_bounded_identity_ids(lineage.get("job_ids"), 200, "employer discovery identity")).issubset(identity.get("observed_job_ids", []))):
            raise ValueError("Employer discovery identity differs from the observed profile jobs")
        identity["employer_discovery_lineage"] = copy.deepcopy(lineage)
    return identity


def _bounded_identity_ids(value, maximum, context):
    if (not isinstance(value, list) or not 1 <= len(value) <= maximum
            or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("Invalid " + context + " collection")
    return list(value)


def _validate_continuity(continuity, identity, step, status, segment):
    if not isinstance(continuity, dict) or not set(continuity).issubset(CONTINUITY_KEYS):
        raise ValueError("Invalid continuity identifiers")
    if 23 <= step <= 31 and "application_id" in continuity:
        raise ValueError("Employer discovery continuity must not assert application identity")
    for key, value in continuity.items():
        if key == "document_ids":
            documents = _bounded_identity_ids(value, 10, "continuity document identity")
            if not set(documents).issubset(identity.get("document_ids", [])):
                raise ValueError("Numbered observation continuity document identity mismatch")
        elif not isinstance(value, str) or not SAFE_ID.fullmatch(value):
            raise ValueError("Invalid numbered observation continuity identity")
        elif key == "document_id":
            if value not in identity.get("document_ids", []):
                raise ValueError("Numbered observation continuity document identity mismatch")
        elif key == "job_id":
            # Browsing may inspect A; saving/preparing B must stay tied to B.
            # Including any application-scoped identifier also asserts that tie.
            application_scope = step >= 32 or "application_id" in continuity or (
                not 23 <= step <= 31 and any(name in continuity for name in IDENTIFIERS[1:]))
            if ((application_scope and value != identity.get("job_id"))
                    or (not application_scope and value not in identity.get("observed_job_ids", []))):
                raise ValueError("Numbered observation continuity job identity mismatch")
            if 23 <= step <= 31 and value not in identity.get("employer_discovery_lineage", {}).get("job_ids", []):
                raise ValueError("Employer observation continuity job was not observed by that discovery")
        elif 23 <= step <= 31 and key in {"operation_id", "request_id"}:
            if value != identity.get("employer_discovery_lineage", {}).get(key):
                raise ValueError("Employer observation continuity discovery operation identity mismatch")
        elif identity.get(key) != value:
            raise ValueError("Numbered observation continuity identity mismatch")
    if ("document_id" in continuity and "document_ids" in continuity
            and continuity["document_id"] not in continuity["document_ids"]):
        raise ValueError("Numbered observation continuity document identities conflict")
    if status == "PASS":
        required = set(IDENTIFIERS if step >= 54 or segment == "public" else
                       ("profile_id", "application_id") if step >= 33 else
                       ("profile_id",) if step >= 3 else ())
        if 17 <= step <= 53:
            required.add("job_id")
        if 23 <= step <= 31:
            required.update(("operation_id", "request_id"))
        if step in {33, 34}:
            required.add("document_ids")
        elif step in {35, 41, 42}:
            required.add("document_id")
        if not required.issubset(continuity):
            raise ValueError("PASS observation is missing continuity identity")
        if step == 34 and len(continuity["document_ids"]) < 2:
            raise ValueError("Comparison continuity requires distinct base and tailored document identities")


def validate_runtime_identity(value):
    if (not isinstance(value, dict) or set(value) != {"source_manifest_sha256", "images"}
            or not isinstance(value["source_manifest_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", value["source_manifest_sha256"]) is None
            or not isinstance(value["images"], dict) or not 1 <= len(value["images"]) <= 20
            or "api" not in value["images"]
            or any(not isinstance(key, str) or not SAFE_ID.fullmatch(key)
                   or not isinstance(image, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None
                   for key, image in value["images"].items())):
        raise ValueError("Invalid producer runtime identity")
    return copy.deepcopy(value)


def validate_stage_lineage(artifact, identity):
    values = artifact.get("stage_lineage", [])
    fields = {"step", "operation_id", "request_id", "operation_kind", "application_id", "profile_id", "job_id"}
    if not isinstance(values, list) or len(values) > 18:
        raise ValueError("Invalid application stage lineage")
    by_step = {}
    for row in values:
        if (not isinstance(row, dict) or set(row) != fields or type(row["step"]) is not int
                or not 36 <= row["step"] <= 53 or row["step"] in by_step
                or row["operation_kind"] not in {"PREPARE", "APPLY_MOCK"}
                or row["step"] >= 44 and row["operation_kind"] != "APPLY_MOCK"
                or any(not isinstance(row[key], str) or not SAFE_ID.fullmatch(row[key]) for key in fields - {"step"})
                or any(row[key] != identity.get(key) for key in ("profile_id", "application_id", "job_id"))
                or row["step"] >= 49 and any(row[key] != identity.get(key) for key in ("operation_id", "request_id"))):
            raise ValueError("Application stage lineage scope or operation identity mismatch")
        by_step[row["step"]] = copy.deepcopy(row)
    return by_step


def _public_source_module():
    # Import only after explicit opt-in. No provider call occurs on default CLI.
    from cli import acceptance_public_source
    return acceptance_public_source


def _monitoring_module():
    from cli import acceptance_monitoring
    return acceptance_monitoring


def _public_browser_module():
    try:
        from cli import acceptance_public_browser
    except ImportError as error:
        if "acceptance_public_browser" not in str(error):
            raise
        return None
    return acceptance_public_browser


def _segment_names(public_provider, monitoring):
    return ("ui",) + (("public",) if public_provider else ()) + (
        ("monitor_prepare",) if monitoring else ()) + ("recovery",) + (("monitor_after",) if monitoring else ())


def _baseline_reference(binding):
    return binding.get("monitoring_baseline", binding.get("derived_baseline", binding["segments"]["ui"]))


def _read_reference(directory, reference):
    if reference.get("artifact") not in {"ui.json", "recovery-baseline.json", "monitoring-baseline.json",
                                         "monitor_prepare.json", "recovery.json"}:
        raise ValueError("Invalid journey evidence reference")
    value, sha = _read_artifact(Path(directory) / reference["artifact"])
    if sha != reference.get("sha256"):
        raise ValueError("Journey evidence reference hash mismatch")
    return value


def validate_public_artifact(artifact, baseline, provider, run_id):
    if (artifact.get("result") != "PASS" or artifact.get("provider") != provider
            or artifact.get("execution_scope") != "LIVE_PUBLIC_READ_ONLY"
            or artifact.get("external_submission_enabled") is not False
            or artifact.get("source") != baseline["source"]
            or artifact.get("acceptance_run_id") != run_id
            or any(artifact.get(key) != baseline[key] for key in IDENTIFIERS)
            or artifact.get("cleanup", {}).get("status") not in {"RESTORED_DISABLED", "NOT_REQUIRED"}):
        raise ValueError("Same-profile public observation or safe cleanup did not complete")
    observed = artifact.get("observed_real_job_ids")
    selected = artifact.get("selected_application_job_id")
    if (not isinstance(observed, list) or not 1 <= len(observed) <= 5
            or any(not isinstance(value, str) or not SAFE_ID.fullmatch(value) for value in observed)
            or len(set(observed)) != len(observed)
            or not isinstance(selected, str) or not SAFE_ID.fullmatch(selected) or selected in observed
            or (baseline.get("selected_application_job_id") is not None
                and selected != baseline["selected_application_job_id"])):
        raise ValueError("Real public jobs must remain distinct from the selected mock application job")
    if any(not isinstance(artifact.get(key), str) or not SAFE_ID.fullmatch(artifact[key])
           for key in ("collection_operation_id", "collection_request_id", "source_id")):
        raise ValueError("Public collection source and operation identity is missing")
    if (not isinstance(artifact.get("source_policy_sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", artifact["source_policy_sha256"]) is None):
        raise ValueError("Public collection source policy identity is missing")
    rows = numbered_observations(artifact, segment="public_source", run_id=run_id,
                                source=baseline["source"], identity=_identity(baseline))
    if len(rows) != 1 or rows[0]["step"] != 11 or rows[0]["status"] != "PASS":
        raise ValueError("Same-profile public collection lacks its exact positive observation")
    return artifact


def join_public_browser(baseline, public, browser, public_sha, browser_sha, runtime):
    """Join reviewed read-only browser assertions to collected posting provenance."""
    projection = {key: runtime[key] for key in ("source_manifest_sha256", "images", "dockerfile_sha256", "frontend_assets")}
    keys = (*IDENTIFIERS, "source", "acceptance_run_id", "selected_application_job_id", "observed_real_job_ids",
            "collection_operation_id", "collection_request_id", "source_id", "provider", "execution_scope")
    if (type(browser.get("schema_version")) is not int or browser["schema_version"] != 1
            or browser.get("result") != "PASS" or browser.get("evidence_kind") != "PUBLIC_BROWSER_READ_ONLY"
            or any(browser.get(key) != public.get(key) for key in keys)
            or browser.get("runtime_identity") != projection or browser.get("runtime_identity_sha256") != digest(projection)
            or browser.get("sha256") != digest({key: value for key, value in browser.items() if key != "sha256"})):
        raise ValueError("Public browser provenance, source, runtime or read-only identity mismatch")
    controls = browser.get("browser", {})
    if (controls.get("loopback_only") is not True or controls.get("get_head_only") is not True
            or controls.get("external_requests") != 0 or controls.get("page_errors") != 0
            or type(controls.get("blocked_write_attempts")) is not int or not 0 <= controls["blocked_write_attempts"] <= 10000
            or not isinstance(controls.get("blocked_write_paths"), list)
            or len(controls["blocked_write_paths"]) != min(20, controls["blocked_write_attempts"])):
        raise ValueError("Public browser read-only controls are invalid")
    readback = browser.get("baseline_readback", {})
    if (readback.get("unchanged") is not True or readback.get("before_sha256") != readback.get("after_sha256")
            or readback.get("profile_version") != public.get("profile_version")
            or re.fullmatch(r"[a-f0-9]{64}", str(readback.get("before_sha256"))) is None
            or readback.get("selected_application_job_id") != public["selected_application_job_id"]
            or readback.get("selected_application_route_id") != public.get("selected_application_route_id")
            or set(readback.get("document_ids", [])) != set(baseline.get("application_document_ids", []))):
        raise ValueError("Public browser changed the selected application or documents")
    records = browser.get("acceptance_observations", [])
    if not isinstance(records, list) or len(records) != 2 or {row.get("step") for row in records} != {11, 16}:
        raise ValueError("Public browser requires explicit visibility and restriction observations")
    rows = []
    for item in records:
        step = item["step"]
        check = "public_jobs_visible_in_same_profile_ui" if step == 11 else "source_remote_restriction_visible"
        checks = item.get("checks", {})
        if (set(checks) != {check} or type(checks[check]) is not bool or item.get("status") not in STATUSES
                or item.get("status") == "PASS" and (checks[check] is not True or browser["execution_scope"] != "LIVE_PUBLIC_READ_ONLY")):
            raise ValueError("Public browser named assertion is invalid")
        named = [{"name": check, "passed": checks[check]}]
        if step == 11:
            named.append({"name": "same_profile_public_readonly_jobs_observed", "passed": True})
        rows.append({"step": step, "status": item["status"], "checks": named,
            "observable_result": "Collected real jobs visible in the same profile UI" if step == 11 else "Provider-authored remote restriction visibility observed",
            "evidence_kind": "PUBLIC_READ_ONLY", "source_commit": baseline["source"]["commit"],
            "run_id": public["acceptance_run_id"], "continuity": {key: baseline[key] for key in IDENTIFIERS}})
    timestamp = browser.get("timestamp_utc")
    if not _instant(public["timestamp"]) <= _instant(timestamp) <= datetime.now(timezone.utc) + timedelta(seconds=5):
        raise ValueError("Public browser evidence predates collection or is future-dated")
    return {"schema_version": 1, "result": "PARTIAL", "timestamp": timestamp, "source": baseline["source"],
        "acceptance_run_id": public["acceptance_run_id"], "public_artifact": "public.json", "public_sha256": public_sha,
        "browser_artifact": "public-browser.json", "browser_sha256": browser_sha,
        "runtime_identity": projection, "acceptance_observations": rows}


def _validate_recaptured_baseline(original, updated):
    _identity(updated)
    for key in ("selected_application_job_id", "selected_application_route_id"):
        if key in original and updated.get(key) != original[key]:
            raise ValueError("Public collection changed the selected application identity")
    mutable = {"job_inventory", "local_persistence", "selected_application_job_id",
               "selected_application_route_id", "baseline_lineage", "baseline_recaptured_at_utc"}
    if any(original.get(key) != updated.get(key) for key in set(original) | set(updated) if key not in mutable):
        raise ValueError("Public collection changed a protected UI baseline field")
    before = persistence.validate(original["local_persistence"])
    after = persistence.validate(updated["local_persistence"])
    for name in persistence.DOMAINS:
        if name != "company_source_catalog" and before["domains"][name] != after["domains"][name]:
            raise ValueError("Public collection changed protected persistence domain: " + name)
    inventory = recovery.validate_public_job_inventory(updated["job_inventory"])
    if inventory["profile_id"] != original["profile_id"]:
        raise ValueError("Public collection inventory changed profile")
    return updated


def recapture_public_baseline(original, public, target):
    """Extend jobs/catalog snapshots while proving profile, docs and app unchanged."""
    with httpx.Client(base_url=target["base_url"], timeout=15, trust_env=False,
                      follow_redirects=False) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()
        application = get("/profiles/" + original["profile_id"] + "/applications/" + original["application_id"])
        if (application.get("job_id") != public["selected_application_job_id"]
                or application.get("route_id") != public["selected_application_route_id"]
                or application.get("target") != "MOCK"):
            raise ValueError("Public collection changed the selected mock application route")
        updated = copy.deepcopy(original)
        updated.update(job_inventory=recovery.collect_public_job_inventory(get, original["profile_id"]),
                       local_persistence=persistence.observe_http(client, original),
                       selected_application_job_id=public["selected_application_job_id"],
                       selected_application_route_id=public["selected_application_route_id"],
                       baseline_recaptured_at_utc=utc_now())
    return _validate_recaptured_baseline(original, updated)


def observe_recovery_readbacks(target, baseline):
    """Use existing public validators after the actual controlled service restart."""
    with httpx.Client(base_url=target["base_url"], timeout=15, trust_env=False,
                      follow_redirects=False) as client:
        def request(method, path):
            response = client.request(method, path)
            response.raise_for_status()
            return response.json()
        recovery.verify_recovered_application(request, baseline)
        application = request("GET", "/profiles/" + baseline["profile_id"] + "/applications/" + baseline["application_id"])
        if (application.get("id") != baseline["application_id"]
                or application.get("profile_id") != baseline["profile_id"]
                or application.get("target") != "MOCK"
                or (baseline.get("selected_application_job_id") is not None
                    and application.get("job_id") != baseline["selected_application_job_id"])):
            raise ValueError("Recovered application identity changed")
        return {"application_recovered": True,
                "job_inventory": recovery.collect_public_job_inventory(lambda path: request("GET", path), baseline["profile_id"]),
                "local_persistence": persistence.observe_http(client, baseline)}


def bind_recovery_evidence(baseline, raw_path, run_id, target, before, after):
    """Emit 54-58 from explicit restart and public readback predicates, not prose."""
    raw_path = Path(raw_path).resolve()
    raw, raw_sha = _read_artifact(raw_path)
    identity = _identity(baseline)
    if raw.get("result") != "PASS" or raw.get("external_submission_enabled") is not False:
        raise ValueError("Controlled recovery did not finish safely")
    expected_services = set(recovery.RECOVERY_RESTART_SERVICES)
    health = raw.get("container_health", {})
    healthy = (set(health.get("after", {})) == expected_services
               and all(row == {"status": "running", "health": "healthy"} for row in health["after"].values()))
    before_starts, after_starts = before.get("service_started_at", {}), after.get("service_started_at", {})
    restarted = (set(before_starts) == set(after_starts) == expected_services
                 and all(_instant(before_starts[name]) < _instant(after_starts[name]) <= _instant(raw["timestamp"])
                         for name in expected_services)
                 and set(raw.get("compose_target", {}).get("services", [])) == expected_services)
    inventory_result = raw.get("public_job_inventory_recovery", {})
    inventory_proof = inventory_result.get("result") == "PASS"
    persisted = raw.get("local_persistence", {})
    persistence_proof = (persisted.get("result") == "PASS"
                         and set(persisted.get("domains", [])) == set(persistence.DOMAINS))
    observed = observe_recovery_readbacks(target, baseline) if inventory_proof or persistence_proof else None
    if inventory_proof:
        expected = recovery.validate_public_job_inventory(baseline.get("job_inventory"))
        if any(inventory_result.get(key) != expected[key]
               for key in ("profile_id", "sha256", "job_count", "source_copy_count")):
            raise ValueError("Recovery inventory receipt differs from its exact baseline")
        recovery.compare_public_job_inventory(baseline, observed["job_inventory"], observed["job_inventory"])
    if persistence_proof:
        persistence.compare_baseline(baseline, observed["local_persistence"])
        if observed.get("application_recovered") is not True:
            raise ValueError("Recovered mock application receipt was not observed")
    recovered_stack = restarted and healthy
    available = {54: recovered_stack, 55: recovered_stack and inventory_proof,
                 56: recovered_stack and persistence_proof, 57: recovered_stack and persistence_proof,
                 58: recovered_stack and persistence_proof}
    observations = [{"step": step, "status": "PASS" if available[step] else "NOT_RUN",
                     "observable_result": ("Exact same-run recovery predicate observed" if available[step]
                                           else "Required restart or domain-specific readback is missing"),
                     "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": baseline["source"]["commit"],
                     "run_id": run_id, "continuity": {key: identity[key] for key in IDENTIFIERS},
                     "checks": [{"name": name, "passed": bool(available[step])} for name in sorted(REQUIRED_CHECKS[step])]}
                    for step in range(54, 59)]
    return {**copy.deepcopy(raw), "schema_version": 1, "scope": "CONTROLLED_RECOVERY_SEGMENT",
            "source": baseline["source"], "acceptance_run_id": run_id,
            **{key: identity[key] for key in IDENTIFIERS},
            "completed_at_utc": raw["timestamp"], "recovery_artifact": "compose-recovery.json",
            "recovery_artifact_sha256": raw_sha, "recovery_artifact_path": str(raw_path),
            "baseline_sha256": digest(baseline),
            "service_started_at": {"before": before_starts, "after": after_starts},
            "post_recovery_readbacks": observed,
            "acceptance_observations": observations}


def _validate_bound_recovery(receipt, baseline, directory):
    path = Path(directory).resolve() / "compose-recovery.json"
    raw, sha = _read_artifact(path)
    if (receipt.get("scope") != "CONTROLLED_RECOVERY_SEGMENT"
            or receipt.get("recovery_artifact_path") != str(path)
            or receipt.get("recovery_artifact_sha256") != sha
            or receipt.get("completed_at_utc") != raw.get("timestamp")
            or receipt.get("baseline_sha256") != digest(baseline)
            or receipt.get("compose_target") != raw.get("compose_target")
            or receipt.get("application_id") != raw.get("application_id")):
        raise ValueError("Bound recovery receipt differs from its actual raw artifact or baseline")
    for key in ("result", "external_submission_enabled", "local_persistence", "public_job_inventory_recovery", "container_health"):
        if receipt.get(key) != raw.get(key):
            raise ValueError("Bound recovery predicate differs from its raw artifact")
    return receipt


def validate_monitoring_result(stage, artifact, baseline, run_id, *, preparation=None, recovery_evidence=None):
    expected_stage = "prepare" if stage == "monitor_prepare" else "after_restart"
    if (artifact.get("stage") != expected_stage or artifact.get("stage_result") != "PASS"
            or artifact.get("result") != "PARTIAL" or artifact.get("external_submission_enabled") is not False
            or artifact.get("source") != baseline["source"] or artifact.get("acceptance_run_id") != run_id
            or artifact.get("baseline_sha256") != digest(baseline)
            or any(artifact.get(key) != baseline[key] for key in IDENTIFIERS)):
        raise ValueError("Monitoring result is not bound to the same immutable input baseline")
    binding = artifact.get("monitoring_binding", {})
    operation = artifact.get("refresh_operation", {})
    receipt = artifact.get("refresh_receipt", {})
    if (binding.get("profile_id") != baseline["profile_id"]
            or not isinstance(binding.get("source_id"), str) or not SAFE_ID.fullmatch(binding["source_id"])
            or operation.get("state") != "COMPLETED" or operation.get("attempt_count") != 1
            or binding.get("completed_operation_id") != operation.get("id")
            or binding.get("operation_id") != operation.get("id")
            or receipt != {"kind": "PUBLIC_COMPLETED_OPERATION_POINTER", "profile_id": baseline["profile_id"],
                           "source_id": binding["source_id"], "operation_id": operation.get("id")}):
        raise ValueError("Monitoring refresh operation and receipt identity mismatch")
    recovery.validate_public_job_inventory(artifact.get("job_inventory"))
    persistence.validate(artifact.get("local_persistence"))
    if stage == "monitor_prepare":
        if artifact.get("observations") != []:
            raise ValueError("Monitoring preparation cannot certify post-restart observations")
        return artifact
    if preparation is None or recovery_evidence is None:
        raise ValueError("Monitoring restart proof requires preparation and actual recovery evidence")
    if (artifact.get("preparation_sha256") != digest(preparation)
            or artifact.get("recovery_evidence_sha256") != digest(recovery_evidence)
            or artifact.get("monitoring_context") != preparation.get("monitoring_context")):
        raise ValueError("Monitoring restart lineage mismatch")
    earlier = preparation["monitoring_binding"]
    for key in ("source_id", "source_version", "profile_id", "profile_version", "marker", "origin",
                "search_fingerprint", "revision", "enabled", "poll_interval", "external_submission_enabled"):
        if binding.get(key) != earlier.get(key):
            raise ValueError("Monitoring configuration changed across recovery")
    due = _instant(earlier["next_due"])
    completed = _instant(recovery_evidence["completed_at_utc"])
    params = operation.get("params", {})
    if (not _instant(preparation["timestamp"]) <= completed < due
            or not completed <= due <= _instant(operation["started_at"]) <= _instant(operation["completed_at"]) <= _instant(artifact["timestamp"])
            or operation.get("id") == preparation["refresh_operation"].get("id")
            or params.get("predecessor_operation_id") != preparation["refresh_operation"].get("id")
            or params.get("scheduled_for") != earlier["next_due"] or _instant(binding["next_due"]) <= due):
        raise ValueError("Monitoring operation was not newly naturally due after final recovery")
    persistence.compare(preparation["local_persistence"], artifact["local_persistence"])
    recovery.compare_public_job_inventory({"job_inventory": preparation["job_inventory"]},
                                         artifact["job_inventory"], artifact["job_inventory"])
    rows = numbered_observations({**artifact, "acceptance_observations": artifact.get("observations", [])},
                                 segment="monitor_after", run_id=run_id, source=baseline["source"], identity=_identity(baseline))
    if {row["step"] for row in rows if row["status"] == "PASS"} != {59, 60}:
        raise ValueError("Monitoring restart proof lacks explicit positive scheduler and monitoring observations")
    return artifact


def monitoring_recovery_baseline(original, preparation, preparation_sha):
    updated = copy.deepcopy(original)
    updated.update(job_inventory=preparation["job_inventory"], local_persistence=preparation["local_persistence"])
    _validate_recaptured_baseline(original, updated)
    updated["monitoring_preparation"] = {
        "artifact": "monitor_prepare.json", "artifact_sha256": preparation_sha,
        "monitoring_binding": preparation["monitoring_binding"],
        "refresh_operation": preparation["refresh_operation"], "refresh_receipt": preparation["refresh_receipt"]}
    return updated


def numbered_observations(artifact, *, segment, run_id, source, identity):
    """Validate explicit producer assertions, never infer numbered checks from prose.

    Artifact paths/hashes are added by the coordinator after producer completion,
    avoiding a self-referential hash. PUBLIC_READ_ONLY is admitted only for the
    explicitly opted-in same-profile public segment; fixture UI cannot earn 11.
    """
    rows = artifact.get("acceptance_observations", artifact.get("observations", []) if segment == "monitor_after" else [])
    if not isinstance(rows, list) or len(rows) > 60:
        raise ValueError("Invalid numbered observation collection")
    if not rows:
        return []
    if artifact.get("acceptance_run_id") != run_id or artifact.get("source") != source:
        raise ValueError("Observation artifact run or source binding mismatch")
    seen = set()
    result = []
    lineage = validate_stage_lineage(artifact, identity)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid numbered observation")
        step = row.get("step")
        if (type(step) is not int or not 1 <= step <= 60 or step in seen
                or (segment == "ui" and (step == 1 or step >= 54))
                or (segment == "startup" and step != 1)
                or (segment in {"public", "public_source"} and step != 11)
                or (segment == "public_browser" and step not in {11, 16})
                or (segment == "recovery" and not 54 <= step <= 58)
                or (segment == "monitor_after" and step not in {59, 60})
                or segment == "monitor_prepare"):
            raise ValueError("Duplicate or out-of-segment numbered observation")
        seen.add(step)
        if row.get("run_id") != run_id or row.get("source_commit") != source["commit"]:
            raise ValueError("Numbered observation run or source mismatch")
        status = row.get("status")
        kind = "PUBLIC_READ_ONLY" if segment in {"public", "public_source", "public_browser"} else "CONTROLLED_FIXTURE"
        if status not in STATUSES or row.get("evidence_kind") != kind:
            raise ValueError("Unsupported observation status or source semantics")
        observable = row.get("observable_result")
        if not isinstance(observable, str) or not observable.strip() or len(observable) > 500:
            raise ValueError("Numbered observation requires bounded sanitized observable result")
        continuity = row.get("continuity")
        _validate_continuity(continuity, identity, step, status, segment)
        if status == "PASS":
            if step == 11 and segment not in {"public", "public_source", "public_browser"}:
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
            required_checks = ({"same_profile_public_readonly_jobs_observed"} if segment == "public_source" else
                               {"source_remote_restriction_visible"} if segment == "public_browser" and step == 16 else REQUIRED_CHECKS.get(step))
            if (required_checks is None or set(names) != required_checks
                    or len(names) != len(set(names))):
                raise ValueError("PASS requires exactly the registered step-specific checks")
            if 36 <= step <= 53:
                validate_runtime_identity(artifact.get("runtime_identity"))
            if 36 <= step <= 53 and step not in lineage:
                raise ValueError("Application observation requires actual stage lineage")
        result.append({"step": step, "status": status, "observable_result": observable,
                       "evidence_kind": kind, "source_commit": source["commit"],
                       "run_id": run_id, "continuity": dict(continuity), "checks": copy.deepcopy(checks)})
    return result


class JourneyCheckpoint(ui_e2e.EvidenceJournal):
    """Reuse the existing fsync + replace journal for bounded resumable state."""

    @classmethod
    def create(cls, path, run_id, source, target, public_source_provider=None, monitoring=False):
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ValueError("Invalid journey run identity")
        return cls(path, {"schema_version": 1, "requirement_id": REQUIREMENT,
                         "run_id": run_id, "source": validate_clean_source(source),
                         "target": target, "started_at_utc": utc_now(), "result": "PARTIAL",
                         "review_status": "NOT_RUN", "frontend_build": {"status": "NOT_RUN"},
                         "trust_boundary": "Reviewed exact-source producers and operator-owned local artifacts; observed PASS is not independent review or ledger acceptance.",
                         "public_source_provider": public_source_provider,
                         "monitoring": monitoring,
                         "segments": {name: {"status": "NOT_RUN"} for name in _segment_names(public_source_provider, monitoring)},
                         "steps": [{"number": number, "status": "NOT_RUN"} for number in range(1, 61)],
                         "limitations": [
                             "No public-source criterion 11 acceptance from controlled fixtures.",
                             "Only explicit numbered producer observations receive credit.",
                             "Frontend bundle build provenance requires a separate build receipt."]})

    @classmethod
    def resume(cls, path, run_id, source, target, public_source_provider=None, monitoring=False):
        saved, _ = _read_artifact(path)
        if (type(saved.get("schema_version")) is not int or saved.get("schema_version") != 1
                or saved.get("requirement_id") != REQUIREMENT
                or saved.get("run_id") != run_id or saved.get("source") != validate_clean_source(source)
                or saved.get("target") != target or saved.get("public_source_provider") != public_source_provider
                or saved.get("monitoring", False) is not monitoring):
            raise ValueError("Checkpoint run, source or target identity mismatch")
        segments = saved.get("segments")
        names = _segment_names(public_source_provider, monitoring)
        if not isinstance(segments, dict) or set(segments) != set(names):
            raise ValueError("Invalid checkpoint segments")
        if any(segment.get("status") not in {"COMPLETE", "NOT_RUN"} for segment in segments.values()):
            raise ValueError("Cannot resume an interrupted or failed segment; reconcile and start a new run")
        restored = cls.create(path, run_id, source, target, public_source_provider, monitoring)
        restored.binding["started_at_utc"] = saved["started_at_utc"]
        restored.binding["runtime"] = saved.get("runtime")
        restored.binding["runtime_checks"] = saved.get("runtime_checks", {})
        restored.binding["frontend_build"] = saved.get("frontend_build", {"status": "NOT_RUN"})
        restored.binding["resume_imported"] = True
        for key in ("public_browser", "public_observations"):
            if key in saved:
                restored.binding[key] = saved[key]
        if "startup" in saved:
            startup, startup_sha = _read_artifact(Path(path).parent / "startup.json")
            if saved["startup"] != {"artifact": "startup.json", "sha256": startup_sha}:
                raise ValueError("Startup evidence hash mismatch")
            restored.record_startup(startup, startup_sha, imported=True)
        if "derived_baseline" in saved:
            derived = saved["derived_baseline"]
            if derived.get("artifact") != "recovery-baseline.json":
                raise ValueError("Derived baseline path mismatch")
            updated, sha = _read_artifact(Path(path).parent / "recovery-baseline.json")
            original, ui_sha = _read_artifact(Path(path).parent / "ui.json")
            _, public_sha = _read_artifact(Path(path).parent / "public.json")
            if (sha != derived.get("sha256") or updated.get("baseline_lineage") != {
                    "ui_sha256": ui_sha, "public_sha256": public_sha}):
                raise ValueError("Derived baseline artifact lineage mismatch")
            _validate_recaptured_baseline(original, updated)
            restored.binding["derived_baseline"] = derived
        for key in ("monitoring_input_baseline", "monitoring_baseline"):
            if key in saved:
                _read_reference(Path(path).parent, saved[key])
                restored.binding[key] = saved[key]
        if "monitoring_baseline" in saved:
            original = _read_reference(Path(path).parent, saved["monitoring_input_baseline"])
            preparation, prep_sha = _read_artifact(Path(path).parent / "monitor_prepare.json")
            expected = monitoring_recovery_baseline(original, preparation, prep_sha)
            if _read_reference(Path(path).parent, saved["monitoring_baseline"]) != expected:
                raise ValueError("Monitoring baseline lineage mismatch")
        for name in names:
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
                    if row.get("verification", {}).get("evidence_artifact") in ({f"{name}.json", "public-observations.json"} if name == "public" else {f"{name}.json"}):
                        row["verification"]["resume_imported"] = True
        return restored

    def payload(self):
        return copy.deepcopy(self.binding)

    def record_startup(self, artifact, sha, *, imported=False):
        receipt, receipt_sha = _read_artifact(self.path.parent / "startup-receipt.json")
        if (artifact.get("receipt_artifact") != "startup-receipt.json" or artifact.get("receipt_sha256") != receipt_sha
                or artifact.get("source") != self.binding["source"] or artifact.get("acceptance_run_id") != self.binding["run_id"]
                or artifact.get("timestamp") != receipt.get("completed_at_utc")):
            raise ValueError("Startup evidence lineage mismatch")
        row = validate_startup_receipt(receipt, self.binding["source"], self.binding["run_id"], self.binding["target"], artifact["runtime"])
        if artifact.get("acceptance_observations") != [row]:
            raise ValueError("Startup numbered observation mismatch")
        self.binding["startup"] = {"artifact": "startup.json", "sha256": sha}
        self.binding["steps"][0] = {"number": 1, "status": "PASS", "observation": row, "verification": {
            "tested_commit": self.binding["source"]["commit"], "evidence_artifact": "startup.json", "evidence_sha256": sha,
            "run_id": self.binding["run_id"], "evidence_kind": "CONTROLLED_FIXTURE", "continuity": {},
            "command": receipt["command"], "observed_result": row["observable_result"],
            "executed_at_utc": artifact["timestamp"], "resume_imported": imported}}

    def begin(self, name):
        if name not in self.binding["segments"] or self.binding["segments"][name]["status"] != "NOT_RUN":
            raise ValueError("Segment cannot be replayed")
        if name != "ui" and self.binding["segments"]["ui"]["status"] != "COMPLETE":
            raise ValueError("Recovery requires a completed connected UI baseline")
        if name == "recovery" and self.binding.get("public_source_provider") and (
                self.binding["segments"]["public"]["status"] != "COMPLETE"
                or "derived_baseline" not in self.binding):
            raise ValueError("Recovery requires the completed public stage and derived baseline")
        names = _segment_names(self.binding.get("public_source_provider"), self.binding.get("monitoring", False))
        if any(self.binding["segments"][prior]["status"] != "COMPLETE" for prior in names[:names.index(name)]):
            raise ValueError("Journey segments must complete in connected order")
        self.binding["active_segment"] = name
        self.binding["segments"][name] = {"status": "IN_PROGRESS", "started_at_utc": utc_now()}
        self.persist()

    def complete_segment(self, name, artifact, sha, *, persist=True):
        segment_sha = sha
        observation_segment = name
        evidence_name = f"{name}.json"
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
            if "employer_discovery_lineage" in identity:
                self.binding["employer_discovery_lineage"] = copy.deepcopy(identity["employer_discovery_lineage"])
            lineage = validate_stage_lineage(artifact, identity)
            if "runtime_identity" in artifact:
                expected_runtime = {key: self.binding["runtime"][key] for key in ("source_manifest_sha256", "images")}
                if validate_runtime_identity(artifact["runtime_identity"]) != expected_runtime:
                    raise ValueError("UI runtime identity differs from validated coordinator runtime")
                self.binding["runtime_identity"] = artifact["runtime_identity"]
            self.binding["stage_lineage"] = list(lineage.values())
        elif name == "public":
            original, _ = _read_artifact(self.path.parent / "ui.json")
            validate_public_artifact(artifact, original, self.binding["public_source_provider"], self.binding["run_id"])
            identity = self.binding["continuity"]
            self.binding["observed_real_job_ids"] = artifact["observed_real_job_ids"]
            self.binding["selected_application_job_id"] = artifact["selected_application_job_id"]
            if "public_browser" in self.binding:
                browser, browser_sha = _read_artifact(self.path.parent / "public-browser.json")
                joined, joined_sha = _read_artifact(self.path.parent / "public-observations.json")
                if (self.binding["public_browser"] != {"artifact": "public-browser.json", "sha256": browser_sha}
                        or self.binding["public_observations"] != {"artifact": "public-observations.json", "sha256": joined_sha}
                        or joined != join_public_browser(original, artifact, browser, sha, browser_sha, self.binding["runtime"])):
                    raise ValueError("Public browser joined evidence lineage mismatch")
                artifact, sha = joined, joined_sha
                observation_segment, evidence_name = "public_browser", "public-observations.json"
            else:
                artifact = copy.deepcopy(artifact)
                for row in artifact.get("acceptance_observations", []):
                    if row["step"] == 11 and row["status"] == "PASS":
                        row["status"] = "PARTIAL"
                        row["checks"].append({"name": "public_jobs_visible_in_same_profile_ui", "passed": False})
        elif name in {"monitor_prepare", "monitor_after"}:
            identity = self.binding["continuity"]
            original = _read_reference(self.path.parent, self.binding["monitoring_input_baseline"])
            preparation = receipt = None
            if name == "monitor_after":
                preparation = _read_reference(self.path.parent, self.binding["segments"]["monitor_prepare"])
                receipt = _read_reference(self.path.parent, self.binding["segments"]["recovery"])
                _validate_bound_recovery(receipt, _read_reference(self.path.parent, _baseline_reference(self.binding)), self.path.parent)
            validate_monitoring_result(name, artifact, original, self.binding["run_id"],
                                       preparation=preparation, recovery_evidence=receipt)
            artifact = {**artifact, "acceptance_observations": artifact.get("observations", [])}
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
            if artifact.get("scope") == "CONTROLLED_RECOVERY_SEGMENT":
                _validate_bound_recovery(artifact, _read_reference(self.path.parent, _baseline_reference(self.binding)), self.path.parent)
            if any(row.get("step") == 55 and row.get("status") == "PASS"
                   for row in artifact.get("acceptance_observations", [])):
                baseline_name = _baseline_reference(self.binding).get("artifact", "ui.json")
                baseline, _ = _read_artifact(self.path.parent / baseline_name)
                inventory = baseline.get("job_inventory")
                recovery.validate_public_job_inventory(inventory)
                if inventory.get("schema_version") != recovery.JOB_INVENTORY_VERSION:
                    raise ValueError("Criterion 55 requires current complete baseline inventory")
                recovered = artifact.get("public_job_inventory_recovery", {})
                if any(recovered.get(key) != inventory[key]
                       for key in ("profile_id", "sha256", "job_count", "source_copy_count")):
                    raise ValueError("Criterion 55 inventory differs from the connected UI baseline")
        rows = numbered_observations(artifact, segment=observation_segment, run_id=self.binding["run_id"],
                                    source=self.binding["source"], identity=identity)
        for row in rows:
            report_row = {
                "number": row["step"], "status": row["status"], "observation": row,
                "verification": {"tested_commit": self.binding["source"]["commit"],
                                 "evidence_artifact": evidence_name, "evidence_sha256": sha,
                                 "run_id": self.binding["run_id"], "evidence_kind": row["evidence_kind"],
                                 "command": {"ui": "python -m cli.ui_e2e --onboarding",
                                             "public": "cli.acceptance_public_source.run (explicit contained opt-in)",
                                             "monitor_prepare": "cli.acceptance_monitoring.prepare (explicit opt-in)",
                                             "monitor_after": "cli.acceptance_monitoring.after_restart (explicit opt-in)",
                                             "recovery": "python -m cli.compose_recovery --baseline " +
                                                 _baseline_reference(self.binding).get("artifact", "ui.json")}[name],
                                 "observed_result": row["observable_result"],
                                 "executed_at_utc": artifact["timestamp"],
                                 "continuity": row["continuity"]}}
            if name == "ui" and row["status"] == "PASS" and self.binding["frontend_build"]["status"] != "PASS":
                report_row.update(status="PARTIAL", producer_status="PASS", credit_withheld="FRONTEND_BUILD_NOT_BOUND")
            if name == "public" and "public_browser" not in self.binding and row["step"] == 11:
                report_row.update(status="PARTIAL", producer_status="PASS", credit_withheld="PUBLIC_BROWSER_NOT_OBSERVED")
            if observation_segment == "public_browser" and row["status"] == "PASS" and self.binding["frontend_build"]["status"] != "PASS":
                report_row.update(status="PARTIAL", producer_status="PASS", credit_withheld="FRONTEND_BUILD_NOT_BOUND")
            if "job_id" in row["continuity"]:
                report_row["verification"]["job_scope"] = (
                    "SELECTED_APPLICATION_JOB" if row["step"] >= 32 else "SAME_PROFILE_BROWSED_JOB")
            lineage_row = next((item for item in self.binding.get("stage_lineage", []) if item["step"] == row["step"]), None)
            if lineage_row is not None:
                report_row["verification"]["stage_lineage"] = copy.deepcopy(lineage_row)
            if 23 <= row["step"] <= 31 and "employer_discovery_lineage" in identity:
                report_row["verification"]["employer_discovery_lineage"] = copy.deepcopy(identity["employer_discovery_lineage"])
            self.binding["steps"][row["step"] - 1] = report_row
        self.binding["segments"][name] = {"status": "COMPLETE", "artifact": f"{name}.json", "sha256": segment_sha}
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


def _invoke(producer, args, run_id, *, runtime_identity=None, **kwargs):
    # Older producers are executable but have no numbered acceptance credit.
    if "acceptance_run_id" in inspect.signature(producer).parameters:
        kwargs["acceptance_run_id"] = run_id
    if runtime_identity is not None and "runtime_identity" in inspect.signature(producer).parameters:
        kwargs["runtime_identity"] = copy.deepcopy(runtime_identity)
    producer(*args, **kwargs)


def run(*, base_url, evidence_dir, project_name, port, run_id, resume=False, stop_after_ui=False,
        build_receipt=None, public_source_provider=None, public_source_preflight=None, monitoring=False,
        startup_receipt=None):
    """Execute the fixture flow, optionally one explicit contained public segment.

    Public opt-in is programmatic only. The root-owned callback enforces exclusive
    scheduler, policy and collector ownership. On successful cleanup/recapture it
    receives a final ``release`` stage to restore its own scheduler containment;
    failures retain containment for the operator and never continue recovery.
    """
    if public_source_provider is not None and (
            public_source_provider not in {"wwr", "remotive"} or not callable(public_source_preflight)):
        raise ValueError("Public source opt-in requires an explicit supported provider and ownership containment callback")
    public_module = _public_source_module() if public_source_provider else None
    browser_module = _public_browser_module() if public_source_provider else None
    if type(monitoring) is not bool:
        raise ValueError("Monitoring opt-in must be boolean")
    monitor_module = _monitoring_module() if monitoring else None
    target = validate_target(base_url, project_name, port)
    source = current_source()
    directory = Path(evidence_dir).resolve()
    # Never overwrite an earlier run or put private runtime artifacts in tracked source.
    if directory == ROOT or ROOT in directory.parents and not (ROOT / "outputs") in directory.parents:
        raise ValueError("Journey artifacts must be outside source or inside ignored outputs")
    path = directory / "journey.json"
    if not resume and directory.exists() and any(directory.iterdir()):
        raise ValueError("New journey requires an empty evidence directory")
    checkpoint = (JourneyCheckpoint.resume(path, run_id, source, target, public_source_provider, monitoring) if resume
                  else JourneyCheckpoint.create(path, run_id, source, target, public_source_provider, monitoring))
    runtime = preflight(target, source)
    previous = checkpoint.binding.get("runtime")
    if previous is not None and _runtime_identity(previous) != _runtime_identity(runtime):
        raise ValueError("Runtime source or image identity changed since checkpoint")
    checkpoint.binding["runtime"] = runtime
    if startup_receipt is not None:
        receipt, receipt_sha = _read_artifact(startup_receipt)
        if "startup" in checkpoint.binding:
            _, saved_sha = _read_artifact(directory / "startup-receipt.json")
            if receipt_sha != saved_sha:
                raise ValueError("Cannot replace the imported initial startup receipt")
        else:
            if resume:
                raise ValueError("Resume cannot add new historical startup credit")
            observed = observe_startup_runtime(target, runtime)
            row = validate_startup_receipt(receipt, source, run_id, target, observed)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "startup-receipt.json").open("xb") as stream:
                stream.write(Path(startup_receipt).read_bytes())
            startup = {"schema_version": 1, "result": "PASS", "scope": "OWNED_FIXTURE_STARTUP",
                "source": source, "acceptance_run_id": run_id, "timestamp": receipt["completed_at_utc"],
                "receipt_artifact": "startup-receipt.json", "receipt_sha256": receipt_sha,
                "runtime": observed, "acceptance_observations": [row]}
            JourneyCheckpoint(directory / "startup.json", startup).persist()
            _, startup_sha = _read_artifact(directory / "startup.json")
            checkpoint.record_startup(startup, startup_sha)
    if build_receipt is not None:
        receipt, _ = _read_artifact(build_receipt)
        checkpoint.binding["frontend_build"] = validate_frontend_build_receipt(receipt, source, runtime)
    elif checkpoint.binding["frontend_build"]["status"] == "PASS":
        validate_frontend_build_receipt(checkpoint.binding["frontend_build"]["receipt"], source, runtime)
    checkpoint.persist()
    def source_guard():
        if current_source() != source:
            raise ValueError("Source changed during monitoring")
    try:
        names = _segment_names(public_source_provider, monitoring)
        for name in names:
            if name != "ui" and stop_after_ui:
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
                _invoke(ui_e2e.run, (target["base_url"], artifact_path), run_id, onboarding=True,
                        runtime_identity={key: latest[key] for key in ("source_manifest_sha256", "images")})
            elif name == "public":
                baseline, ui_sha = _read_artifact(directory / "ui.json")
                if ui_sha != checkpoint.binding["segments"]["ui"]["sha256"]:
                    raise ValueError("UI baseline changed before public collection")
                public_module.run(target["base_url"], baseline, artifact_path,
                                  acceptance_run_id=run_id, provider=public_source_provider,
                                  containment_preflight=public_source_preflight)
                public, public_sha = _read_artifact(artifact_path)
                validate_public_artifact(public, baseline, public_source_provider, run_id)
                if browser_module is not None:
                    browser_path = directory / "public-browser.json"
                    joined_path = directory / "public-observations.json"
                    if browser_path.exists() or joined_path.exists():
                        raise ValueError("Refusing to replace public browser evidence")
                    projection = {key: latest[key] for key in ("source_manifest_sha256", "images", "dockerfile_sha256", "frontend_assets")}
                    browser = browser_module.run(target["base_url"], baseline, public, runtime_identity=projection)
                    JourneyCheckpoint(browser_path, browser).persist()
                    _, browser_sha = _read_artifact(browser_path)
                    joined = join_public_browser(baseline, public, browser, public_sha, browser_sha, latest)
                    JourneyCheckpoint(joined_path, joined).persist()
                    _, joined_sha = _read_artifact(joined_path)
                    checkpoint.binding["public_browser"] = {"artifact": browser_path.name, "sha256": browser_sha}
                    checkpoint.binding["public_observations"] = {"artifact": joined_path.name, "sha256": joined_sha}
                updated = recapture_public_baseline(baseline, public, target)
                _validate_recaptured_baseline(baseline, updated)
                inventory_ids = {row["job_id"] for row in updated["job_inventory"]["jobs"]}
                if not set(public["observed_real_job_ids"]).issubset(inventory_ids):
                    raise ValueError("Recaptured inventory is missing observed public jobs")
                updated["baseline_lineage"] = {"ui_sha256": ui_sha, "public_sha256": public_sha}
                derived_path = directory / "recovery-baseline.json"
                if derived_path.exists():
                    raise ValueError("Refusing to replace an existing derived recovery baseline")
                JourneyCheckpoint(derived_path, updated).persist()
                _, derived_sha = _read_artifact(derived_path)
                checkpoint.binding["derived_baseline"] = {"artifact": derived_path.name, "sha256": derived_sha}
                public_source_preflight("release", {"provider": public_source_provider,
                    "base_url": target["base_url"], "acceptance_run_id": run_id,
                    "cleanup_status": public["cleanup"]["status"],
                    "newly_created": public["cleanup"]["status"] == "RESTORED_DISABLED",
                    **{key: public[key] for key in ("collection_operation_id", "collection_request_id",
                                                   "source_id", "source_policy_sha256")}})
            elif name == "monitor_prepare":
                reference = copy.deepcopy(_baseline_reference(checkpoint.binding))
                baseline = _read_reference(directory, reference)
                checkpoint.binding["monitoring_input_baseline"] = reference
                checkpoint.persist()
                monitor_module.prepare(target["base_url"], baseline, artifact_path,
                    acceptance_run_id=run_id, check=source_guard, persistence_root=ROOT)
                preparation, prep_sha = _read_artifact(artifact_path)
                validate_monitoring_result(name, preparation, baseline, run_id)
                derived = monitoring_recovery_baseline(baseline, preparation, prep_sha)
                derived_path = directory / "monitoring-baseline.json"
                if derived_path.exists():
                    raise ValueError("Refusing to overwrite monitoring baseline")
                JourneyCheckpoint(derived_path, derived).persist()
                _, derived_sha = _read_artifact(derived_path)
                checkpoint.binding["monitoring_baseline"] = {"artifact": derived_path.name, "sha256": derived_sha}
            elif name == "monitor_after":
                baseline = _read_reference(directory, checkpoint.binding["monitoring_input_baseline"])
                preparation = _read_reference(directory, checkpoint.binding["segments"]["monitor_prepare"])
                receipt = _read_reference(directory, checkpoint.binding["segments"]["recovery"])
                _validate_bound_recovery(receipt, _read_reference(directory, _baseline_reference(checkpoint.binding)), directory)
                if not any(row["step"] == 54 and row["status"] == "PASS" for row in receipt["acceptance_observations"]):
                    raise ValueError("Monitoring requires observed full-service restart")
                monitor_module.after_restart(target["base_url"], baseline, preparation, artifact_path,
                    acceptance_run_id=run_id, recovery_evidence=receipt, check=source_guard, persistence_root=ROOT)
            else:
                # This is exactly the checkpointed connected UI artifact, not a
                # nearby test receipt or a baseline from another profile/run.
                baseline_ref = _baseline_reference(checkpoint.binding)
                baseline_path = directory / baseline_ref["artifact"]
                baseline, sha = _read_artifact(baseline_path)
                if sha != baseline_ref["sha256"]:
                    raise ValueError("UI baseline changed before recovery")
                _identity(baseline)
                raw_path = directory / "compose-recovery.json"
                if raw_path.exists():
                    raise ValueError("Refusing to reuse a raw recovery artifact")
                _invoke(recovery.run, (baseline_path, raw_path, project_name, port), run_id)
            if current_source() != source:
                raise ValueError("Source changed while producer was executing")
            completed_runtime = preflight(target, source)
            if _runtime_identity(completed_runtime) != _runtime_identity(runtime):
                raise ValueError("Runtime source or image changed while producer was executing")
            if name == "recovery":
                bound = bind_recovery_evidence(baseline, raw_path, run_id, target, latest, completed_runtime)
                JourneyCheckpoint(artifact_path, bound).persist()
            artifact, sha = _read_artifact(artifact_path)
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
    parser.add_argument("--monitoring", action="store_true", help="Explicitly prepare and observe same-profile natural fixture monitoring")
    parser.add_argument("--startup-receipt", help="Actual fresh owned stack startup receipt; absent receipt leaves step 1 NOT_RUN")
    parser.add_argument("--build-receipt", help="Owned actual-build receipt; absent receipt withholds UI observed PASS")
    args = parser.parse_args(argv)
    report = run(**vars(args))
    print(json.dumps({"result": report["result"], "run_id": report["run_id"],
                      "scoreboard": report.get("scoreboard", {})}, sort_keys=True))
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
