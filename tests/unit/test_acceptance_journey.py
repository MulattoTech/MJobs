"""Failure controls for the connected journey coordinator; no browser or Docker."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import httpx

from cli import acceptance_journey as journey


EMPTY_HASH = hashlib.sha256(b"").hexdigest()
SOURCE = {"commit": "a" * 40, "tree": "b" * 40,
          "working_tree_diff_sha256": EMPTY_HASH,
          "working_tree_status_sha256": EMPTY_HASH}
RUN_ID = "mj292-test-run"
TARGET = {"project_name": "mjobs-fixture-012345abcdef", "port": 18777,
          "base_url": "http://127.0.0.1:18777"}
IDENTITY = {"profile_id": "profile-1", "application_id": "application-1",
            "operation_id": "operation-1", "request_id": "request-1"}
RUNTIME = {"source_manifest_sha256": "d" * 64, "compose_files": ["compose.yml"],
           "images": {"api": "sha256:" + "e" * 64}, "containers": {"api": "container-1", "scheduler": "scheduler-1"},
           "frontend_assets": {"index.html": "f" * 64}, "dockerfile_sha256": "2" * 64}


@pytest.fixture(autouse=True)
def bound_archive(monkeypatch):
    monkeypatch.setattr(journey, "source_archive_sha256", lambda _: "1" * 64)
    monkeypatch.setattr(journey, "_public_browser_module", lambda: None)


def build_receipt():
    return {"schema_version": 1, "source_commit": SOURCE["commit"], "source_tree": SOURCE["tree"],
            "image_id": RUNTIME["images"]["api"], "frontend_assets": RUNTIME["frontend_assets"],
            "source_archive_sha256": "1" * 64, "dockerfile_sha256": "2" * 64,
            "result": "PASS", "completed_at_utc": journey.utc_now(), "build_command": ["docker", "build"]}


def observation(step=4):
    return {"step": step, "status": "PASS", "observable_result": "Synthetic resume upload visible",
            "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": SOURCE["commit"],
            "run_id": RUN_ID, "continuity": dict(IDENTITY),
            "checks": [{"name": "resume_upload_and_extraction_visible", "passed": True}]}


def inventory(ids=("mock-job-1",)):
    jobs = [{"job_id": identifier, "canonical_sha256": "a" * 64, "source_copy_count": 1,
             "source_copies_sha256": "b" * 64} for identifier in ids]
    return {"schema_version": 3, "profile_id": IDENTITY["profile_id"], "job_count": len(jobs),
            "source_copy_count": len(jobs), "jobs": jobs, "sha256": journey.recovery._inventory_digest(jobs)}


def baseline():
    return {**IDENTITY, "source": SOURCE, "acceptance_run_id": RUN_ID,
            "timestamp": journey.utc_now(), "result": "PARTIAL", "mode": "ONBOARDING_FIXTURE",
            "target_origin": TARGET["base_url"],
            "external_submission_enabled": False,
            "observed_job_ids": ["browse-job-1", "mock-job-1"],
            "selected_application_job_id": "mock-job-1",
            "application_document_ids": ["resume-1", "base-1", "letter-1"],
            "job_inventory": inventory(),
            "local_persistence": {"schema_version": 1, "domains": {
                name: {"count": 1, "sha256": "a" * 64} for name in journey.persistence.DOMAINS}},
            "confirmation": {"application_id": IDENTITY["application_id"],
                             "receipt": "synthetic-receipt", "submission_count": 1},
            "acceptance_observations": [observation()]}


def recovered():
    return {"application_id": IDENTITY["application_id"], "timestamp": journey.utc_now(),
            "result": "PASS", "external_submission_enabled": False,
            "compose_target": {"project_name": TARGET["project_name"], "port": TARGET["port"],
                               "compose_files": RUNTIME["compose_files"], "profile": "fixture",
                               "project_directory": str(journey.ROOT.resolve())}}


def emit(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def invoke(tmp_path, **kwargs):
    if "build_receipt" not in kwargs:
        receipt_path = tmp_path.with_name(tmp_path.name + "-build.json")
        emit(receipt_path, build_receipt())
        kwargs["build_receipt"] = receipt_path
    return journey.run(base_url=TARGET["base_url"], evidence_dir=tmp_path,
                       project_name=TARGET["project_name"], port=TARGET["port"], run_id=RUN_ID, **kwargs)


def test_clean_source_rejects_missing_dirty_and_unavailable_bindings():
    assert journey.validate_clean_source(SOURCE) == SOURCE
    for invalid in ({}, {**SOURCE, "commit": None},
                    {**SOURCE, "state": "UNAVAILABLE"},
                    {**SOURCE, "working_tree_diff_sha256": "c" * 64},
                    {**SOURCE, "working_tree_status_sha256": "c" * 64}):
        with pytest.raises(ValueError):
            journey.validate_clean_source(invalid)


def test_source_changes_and_run_mismatch_reject_resume(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    checkpoint.persist()
    for run_id, source, target in (("another-run", SOURCE, TARGET),
                                    (RUN_ID, {**SOURCE, "commit": "c" * 40}, TARGET),
                                    (RUN_ID, SOURCE, {**TARGET, "port": 18778})):
        with pytest.raises(ValueError):
            journey.JourneyCheckpoint.resume(checkpoint.path, run_id, source, target)


def test_missing_numbered_observations_do_not_infer_pass_from_actions(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    assert len(checkpoint.payload()["steps"]) == 60
    artifact = {"source": SOURCE, "actions": [
        {"action": "Everything worked, all sixty criteria passed", "result": "PASS"}]}
    assert journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID,
                                         source=SOURCE, identity={}) == []
    assert {step["status"] for step in checkpoint.payload()["steps"]} == {"NOT_RUN"}
    assert checkpoint.payload()["result"] != "PASS"


def test_interrupt_preserves_completed_artifact_without_replaying_uncertain_segment(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    checkpoint.begin("ui")
    checkpoint.fail(KeyboardInterrupt())
    saved = json.loads(checkpoint.path.read_text(encoding="utf-8"))
    assert saved["result"] == "FAIL"
    assert saved["segments"]["ui"]["status"] == "INTERRUPTED"
    assert saved["segments"]["recovery"]["status"] == "NOT_RUN"
    assert {row["status"] for row in saved["steps"]} == {"NOT_RUN"}
    with pytest.raises(ValueError, match="interrupted"):
        journey.JourneyCheckpoint.resume(checkpoint.path, RUN_ID, SOURCE, TARGET)


def test_atomic_checkpoint_preserves_prior_bytes_when_replace_fails(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    checkpoint.persist()
    prior = checkpoint.path.read_bytes()
    with patch("cli.ui_e2e.os.replace", side_effect=OSError("private diagnostic")):
        with pytest.raises(OSError):
            checkpoint.begin("ui")
    assert checkpoint.path.read_bytes() == prior
    assert list(tmp_path.glob("*.tmp")) == []


def test_invalid_target_never_runs_preflight_or_producers(tmp_path):
    with patch.object(journey, "preflight") as preflight, patch.object(journey.ui_e2e, "run") as ui:
        with pytest.raises(ValueError):
            journey.run(base_url="https://example.com", evidence_dir=tmp_path,
                        project_name="production", port=443, run_id=RUN_ID)
    preflight.assert_not_called()
    ui.assert_not_called()


@pytest.mark.parametrize("mutate", [
    lambda a: a["acceptance_observations"][0].update(checks=[]),
    lambda a: a["acceptance_observations"][0]["checks"][0].update(passed=False),
    lambda a: a["acceptance_observations"][0].update(run_id="other-run"),
    lambda a: a["acceptance_observations"][0].update(source_commit="c" * 40),
    lambda a: a["acceptance_observations"][0].update(continuity={}),
    lambda a: a["acceptance_observations"][0]["continuity"].update(profile_id="other-profile"),
    lambda a: a["acceptance_observations"][0].update(step=11),
    lambda a: a["acceptance_observations"][0].update(step=True),
    lambda a: a["acceptance_observations"][0].update(step=61),
    lambda a: a["acceptance_observations"][0].update(step=54),
    lambda a: a["acceptance_observations"][0].update(evidence_kind="PUBLIC_READ_ONLY"),
    lambda a: a.update(source={**SOURCE, "commit": "c" * 40}),
    lambda a: a.update(acceptance_run_id="other-run"),
    lambda a: a["acceptance_observations"].append(copy.deepcopy(a["acceptance_observations"][0])),
])
def test_fabricated_or_cross_run_observations_fail_closed(mutate):
    artifact = baseline()
    mutate(artifact)
    with pytest.raises(ValueError):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID,
                                      source=SOURCE, identity=IDENTITY)


def test_connected_run_uses_the_same_baseline_and_does_not_promote_legacy_recovery(tmp_path):
    def ui(url, path, onboarding=False, acceptance_run_id=None):
        assert url == TARGET["base_url"] and onboarding is True and acceptance_run_id == RUN_ID
        emit(path, baseline())

    def recovery(path, output, project, port):
        assert Path(path) == tmp_path / "ui.json"
        assert json.loads(Path(path).read_text())["profile_id"] == IDENTITY["profile_id"]
        assert (project, port) == (TARGET["project_name"], TARGET["port"])
        emit(output, recovered())

    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=ui) as ui_mock, \
            patch.object(journey.recovery, "run", side_effect=recovery) as recovery_mock:
        # Preserve a real optional-parameter signature while tracking invocation.
        ui_mock.__signature__ = journey.inspect.signature(ui)
        report = invoke(tmp_path)
    assert report["result"] == "PARTIAL"
    assert report["steps"][3]["status"] == "PASS"
    assert report["steps"][10]["status"] == "NOT_RUN"
    assert report["steps"][53]["status"] == "NOT_RUN"
    assert report["scoreboard"]["observed_PASS"] == 1
    assert report["scoreboard"]["NOT_RUN"] == 59
    assert report["segments"]["recovery"]["status"] == "COMPLETE"
    assert report["steps"][3]["verification"]["evidence_sha256"] == hashlib.sha256((tmp_path / "ui.json").read_bytes()).hexdigest()
    assert ui_mock.call_count == recovery_mock.call_count == 1


def test_resume_revalidates_artifact_and_continues_only_recovery(tmp_path):
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())):
        invoke(tmp_path, stop_after_ui=True)
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run") as ui, \
            patch.object(journey.recovery, "run", side_effect=lambda _, path, *args: emit(path, recovered())) as recovery:
        report = invoke(tmp_path, resume=True)
    ui.assert_not_called()
    assert recovery.call_count == 1
    assert report["steps"][3]["status"] == "PASS"


def test_modified_baseline_or_runtime_rejects_resume_before_producer(tmp_path):
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())):
        invoke(tmp_path, stop_after_ui=True)
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value={**RUNTIME, "images": {"api": "sha256:" + "f" * 64}}), \
            patch.object(journey.recovery, "run") as recovery:
        with pytest.raises(ValueError, match="Runtime"):
            invoke(tmp_path, resume=True)
    recovery.assert_not_called()
    with (tmp_path / "ui.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)


def test_interrupted_producer_does_not_run_recovery_or_leak_exception(tmp_path):
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=KeyboardInterrupt("private-token")), \
            patch.object(journey.recovery, "run") as recovery:
        with pytest.raises(KeyboardInterrupt):
            invoke(tmp_path)
    recovery.assert_not_called()
    saved = (tmp_path / "journey.json").read_text()
    assert "private-token" not in saved
    assert json.loads(saved)["segments"]["ui"]["status"] == "INTERRUPTED"


def test_stale_ui_artifact_cannot_be_presented_as_fresh_run(tmp_path):
    stale = baseline()
    stale["timestamp"] = "2000-01-01T00:00:00+00:00"
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, stale)), \
            patch.object(journey.recovery, "run") as recovery:
        with pytest.raises(ValueError, match="timestamp"):
            invoke(tmp_path)
    recovery.assert_not_called()


def test_step55_requires_nested_inventory_pass_even_if_recovery_passes():
    artifact = {**recovered(), "acceptance_run_id": RUN_ID, "source": SOURCE,
                "acceptance_observations": [observation(55)],
                "public_job_inventory_recovery": {"result": "NOT_RUN"}}
    with pytest.raises(ValueError, match="complete inventory"):
        journey.numbered_observations(artifact, segment="recovery", run_id=RUN_ID,
                                      source=SOURCE, identity=IDENTITY)


def test_preflight_rejects_stale_container_source_before_ui(tmp_path):
    source_hashes = {"cli/ui_e2e.py": "a" * 64}
    targets = {name: {"container_id": name} for name in journey.recovery.RECOVERY_RESTART_SERVICES}

    def command(args, **kwargs):
        if kwargs["action"] == "Inspect journey service start":
            return "2000-01-01T00:00:00+00:00"
        if kwargs["action"] == "Inspect journey fixture mode":
            return "MJOBS_OPERATION_MODE=FIXTURE\nMJOBS_COMPOSE_MOCK=1\n"
        return json.dumps("sha256:" + "e" * 64) if args[1] == "inspect" else json.dumps({"cli/ui_e2e.py": "b" * 64})

    with patch.object(journey.recovery, "resolve_compose_files", return_value=(Path("compose.yml"),)), \
            patch.object(journey.recovery, "preflight_recovery_targets", return_value=targets), \
            patch.object(journey, "source_files", return_value=source_hashes), \
            patch.object(journey.recovery, "_run_bounded", side_effect=command), \
            patch.object(journey.ui_e2e, "run") as ui:
        with pytest.raises(ValueError, match="Runtime source bytes"):
            journey.preflight(TARGET, SOURCE)
    ui.assert_not_called()


def test_preflight_refuses_personal_worker_even_with_matching_source():
    source_hashes = {"cli/ui_e2e.py": "a" * 64}
    targets = {name: {"container_id": name} for name in journey.recovery.RECOVERY_RESTART_SERVICES}

    def command(args, **kwargs):
        if kwargs["action"] == "Inspect journey service start":
            return "2000-01-01T00:00:00+00:00"
        if kwargs["action"] == "Inspect journey fixture mode":
            return ""
        return json.dumps("sha256:" + "e" * 64) if args[1] == "inspect" else json.dumps(source_hashes)

    with patch.object(journey.recovery, "resolve_compose_files", return_value=(Path("compose.yml"),)), \
            patch.object(journey.recovery, "preflight_recovery_targets", return_value=targets), \
            patch.object(journey, "source_files", return_value=source_hashes), \
            patch.object(journey.ui_e2e, "_onboarding_preflight"), \
            patch.object(journey.recovery, "_run_bounded", side_effect=command):
        with pytest.raises(ValueError, match="fixture mode"):
            journey.preflight(TARGET, SOURCE)


@pytest.mark.parametrize("mode_output,accepted", [
    ("MJOBS_OPERATION_MODE=FIXTURE\nMJOBS_COMPOSE_MOCK=1\n\n", True),
    ("MJOBS_OPERATION_MODE=FIXTURE\nMJOBS_COMPOSE_MOCK=1\nUNEXPECTED_SAFE_VALUE\n", False),
    ("MJOBS_OPERATION_MODE=FIXTURE\n\n", False),
    ("MJOBS_OPERATION_MODE=FIXTURE\nMJOBS_COMPOSE_MOCK=1\n \n", False),
])
def test_fixture_mode_projection_ignores_only_docker_empty_lines(mode_output, accepted):
    source_hashes = {"cli/ui_e2e.py": "a" * 64, "infra/docker/Dockerfile": "b" * 64}
    targets = {name: {"container_id": name} for name in journey.recovery.RECOVERY_RESTART_SERVICES}
    assets = {"index.html": "c" * 64}
    def command(args, **kwargs):
        action = kwargs["action"]
        if action == "Inspect journey service start":
            return "2000-01-01T00:00:00+00:00"
        if action == "Inspect journey fixture mode":
            return mode_output
        if action == "Inspect journey image":
            return json.dumps("sha256:" + "e" * 64)
        if action == "Hash journey frontend assets":
            return json.dumps(assets)
        assert action == "Verify committed journey runtime source"
        return json.dumps(source_hashes)
    with patch.object(journey.recovery, "resolve_compose_files", return_value=(Path("compose.yml"),)), \
            patch.object(journey.recovery, "preflight_recovery_targets", return_value=targets), \
            patch.object(journey, "source_files", return_value=source_hashes), \
            patch.object(journey.recovery, "_run_bounded", side_effect=command), \
            patch.object(journey.ui_e2e, "_onboarding_preflight") as api, \
            patch.object(journey, "verify_served_assets") as frontend:
        if accepted:
            runtime = journey.preflight(TARGET, SOURCE)
            assert set(runtime["images"]) == set(journey.recovery.RECOVERY_RESTART_SERVICES)
            api.assert_called_once_with(TARGET["base_url"])
            frontend.assert_called_once_with(TARGET["base_url"], assets)
        else:
            with pytest.raises(ValueError, match="fixture mode"):
                journey.preflight(TARGET, SOURCE)
            api.assert_not_called()
            frontend.assert_not_called()


def test_ui_origin_mismatch_is_not_admitted(tmp_path):
    def ui(_, path, **kw):
        value = baseline()
        value["target_origin"] = "http://127.0.0.1:18778"
        emit(path, value)

    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=ui), \
            patch.object(journey.recovery, "run") as recovery:
        with pytest.raises(ValueError, match="origin"):
            invoke(tmp_path)
    recovery.assert_not_called()


def test_unregistered_step_or_wrong_check_cannot_earn_observed_pass():
    for step, name in ((4, "an_authored_true_boolean"), (36, "application_prepared"),
                       (59, "scheduler_process_running"), (60, "stack_restarted")):
        artifact = baseline()
        artifact["acceptance_observations"][0].update(step=step, checks=[{"name": name, "passed": True}])
        if step == 36:
            artifact["acceptance_observations"][0]["continuity"]["job_id"] = "mock-job-1"
        with pytest.raises(ValueError, match="registered"):
            journey.numbered_observations(artifact, segment="monitor_after" if step >= 59 else "ui",
                                          run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))


def test_ui_observation_withholds_pass_without_exact_built_asset_receipt(tmp_path):
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())):
        report = invoke(tmp_path, stop_after_ui=True, build_receipt=None)
    assert report["steps"][3]["status"] == "PARTIAL"
    assert report["steps"][3]["producer_status"] == "PASS"
    assert report["steps"][3]["credit_withheld"] == "FRONTEND_BUILD_NOT_BOUND"
    assert report["scoreboard"]["observed_PASS"] == 0


def test_frontend_receipt_must_bind_source_tree_image_and_actual_assets():
    receipt = build_receipt()
    assert journey.validate_frontend_build_receipt(receipt, SOURCE, RUNTIME)["status"] == "PASS"
    for key, value in (("schema_version", True), ("source_commit", "c" * 40), ("source_tree", "c" * 40),
                       ("image_id", "sha256:" + "c" * 64), ("frontend_assets", {"index.html": "c" * 64}),
                       ("source_archive_sha256", "c" * 64), ("dockerfile_sha256", "c" * 64)):
        with pytest.raises(ValueError, match="frontend"):
            journey.validate_frontend_build_receipt({**receipt, key: value}, SOURCE, RUNTIME)


def test_checkpoint_schema_boolean_is_not_a_version_number(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    checkpoint.binding["schema_version"] = True
    checkpoint.persist()
    with pytest.raises(ValueError, match="identity mismatch"):
        journey.JourneyCheckpoint.resume(checkpoint.path, RUN_ID, SOURCE, TARGET)


def test_resume_retains_original_timestamp_and_marks_imported_observations(tmp_path):
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())):
        first = invoke(tmp_path, stop_after_ui=True)
    restored = journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET).payload()
    assert restored["started_at_utc"] == first["started_at_utc"]
    assert restored["steps"][3]["verification"]["executed_at_utc"] == first["steps"][3]["verification"]["executed_at_utc"]
    assert restored["steps"][3]["verification"]["resume_imported"] is True
    assert restored["review_status"] == "NOT_RUN"


def test_served_asset_hashes_are_actual_bounded_loopback_response_bytes():
    assets = {"index.html": b"<html>synthetic</html>", "assets/main.js": b"synthetic js"}
    expected = {name: hashlib.sha256(value).hexdigest() for name, value in assets.items()}
    requested = []

    def serve(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=assets[request.url.path.lstrip("/") or "index.html"])

    client = httpx.Client(transport=httpx.MockTransport(serve))
    with patch.object(journey.httpx, "Client", return_value=client):
        journey.verify_served_assets(TARGET["base_url"], expected)
    assert len(requested) == 3
    assert all(url.startswith(TARGET["base_url"] + "/") for url in requested)


def test_stale_or_redirected_served_bundle_fails_closed():
    expected = {"index.html": hashlib.sha256(b"expected").hexdigest()}
    for status, data in ((200, b"old bundle"), (302, b"")):
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, content=data)))
        with patch.object(journey.httpx, "Client", return_value=client):
            with pytest.raises(ValueError, match="frontend asset"):
                journey.verify_served_assets(TARGET["base_url"], expected)


def test_asset_manifest_paths_cannot_escape_the_owned_origin():
    for name in ("../outside", "https://example.com", "//example.com", "assets/../secret", "assets\\secret"):
        with patch.object(journey.httpx, "Client") as client, pytest.raises(ValueError):
            journey.verify_served_assets(TARGET["base_url"], {"index.html": "f" * 64, name: "f" * 64})
        client.assert_not_called()


def public_artifact():
    row = observation(11)
    row.update(evidence_kind="PUBLIC_READ_ONLY", checks=[
        {"name": "same_profile_public_readonly_jobs_observed", "passed": True}])
    return {**IDENTITY, "timestamp": journey.utc_now(), "source": SOURCE,
            "acceptance_run_id": RUN_ID, "result": "PASS", "provider": "wwr",
            "collection_operation_id": "collection-op", "collection_request_id": "collection-request", "source_id": "public-source",
            "source_policy_sha256": "7" * 64,
            "profile_version": 1,
            "execution_scope": "LIVE_PUBLIC_READ_ONLY", "external_submission_enabled": False,
            "observed_real_job_ids": ["real-job-1"], "selected_application_job_id": "mock-job-1",
            "selected_application_route_id": "mock-route-1", "cleanup": {"status": "RESTORED_DISABLED"},
            "acceptance_observations": [row]}


def test_public_stage_requires_explicit_provider_and_containment_callback(tmp_path):
    with patch.object(journey.ui_e2e, "run") as ui:
        with pytest.raises(ValueError, match="containment"):
            invoke(tmp_path, public_source_provider="wwr")
    ui.assert_not_called()


@pytest.mark.parametrize("browser_enabled", [False, True])
def test_public_provider_stage_rebases_only_inventory_and_keeps_mock_application(tmp_path, browser_enabled):
    events = []
    def public_run(url, original, path, **kwargs):
        assert kwargs["provider"] == "wwr" and kwargs["acceptance_run_id"] == RUN_ID
        assert original["application_id"] == IDENTITY["application_id"]
        events.append("public")
        emit(path, public_artifact())

    def recapture(original, public, target):
        assert public["observed_real_job_ids"] == ["real-job-1"]
        return {**original, "job_inventory": inventory(("mock-job-1", "real-job-1")),
                "selected_application_job_id": "mock-job-1",
                "selected_application_route_id": "mock-route-1"}

    def browser_run(url, original, public, **kwargs):
        events.append("browser")
        return public_browser_artifact(original, public)

    def recovery(path, output, *args):
        events.append("recovery")
        assert Path(path).name == "recovery-baseline.json"
        value = json.loads(Path(path).read_text())
        assert value["application_id"] == IDENTITY["application_id"]
        assert value["baseline_lineage"]["ui_sha256"] == hashlib.sha256((tmp_path / "ui.json").read_bytes()).hexdigest()
        emit(output, recovered())

    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey, "_public_source_module", return_value=SimpleNamespace(run=public_run)), \
            patch.object(journey, "_public_browser_module", return_value=SimpleNamespace(run=browser_run) if browser_enabled else None), \
            patch.object(journey, "recapture_public_baseline", side_effect=recapture), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())), \
            patch.object(journey.recovery, "run", side_effect=recovery):
        report = invoke(tmp_path, public_source_provider="wwr",
                        public_source_preflight=lambda stage, context: events.append(stage))
    assert events == (["public", "browser", "release", "recovery"] if browser_enabled else ["public", "release", "recovery"])
    assert report["steps"][10]["status"] == ("PASS" if browser_enabled else "PARTIAL")
    if browser_enabled:
        assert report["steps"][15]["status"] == "PASS"
        assert report["steps"][10]["verification"]["evidence_artifact"] == "public-observations.json"
        joined = json.loads((tmp_path / "public-observations.json").read_text())
        assert joined["public_sha256"] == hashlib.sha256((tmp_path / "public.json").read_bytes()).hexdigest()
        assert joined["browser_sha256"] == hashlib.sha256((tmp_path / "public-browser.json").read_bytes()).hexdigest()
    else:
        assert report["steps"][10]["credit_withheld"] == "PUBLIC_BROWSER_NOT_OBSERVED"
    assert report["steps"][10]["observation"]["evidence_kind"] == "PUBLIC_READ_ONLY"
    assert report["selected_application_job_id"] == "mock-job-1"
    assert report["observed_real_job_ids"] == ["real-job-1"]
    assert report["result"] == "PARTIAL"
    restored = journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, "wwr").payload()
    assert restored["steps"][10]["verification"]["resume_imported"] is True
    assert restored["derived_baseline"] == report["derived_baseline"]
    if browser_enabled:
        browser_path = tmp_path / "public-browser.json"
        encoded = browser_path.read_bytes()
        browser_path.write_bytes(encoded + b" ")
        with pytest.raises(ValueError, match="browser joined evidence lineage"):
            journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, "wwr")
        browser_path.write_bytes(encoded)
    with (tmp_path / "recovery-baseline.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="lineage"):
        journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, "wwr")


def test_public_failure_keeps_containment_and_never_restarts_or_recaptures(tmp_path):
    def public_run(_, original, path, **kwargs):
        value = public_artifact()
        value.update(result="FAIL", cleanup={"status": "PRESERVED_POLICY_CHANGED"})
        emit(path, value)

    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey, "_public_source_module", return_value=SimpleNamespace(run=public_run)), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())), \
            patch.object(journey, "recapture_public_baseline") as recapture, \
            patch.object(journey.recovery, "run") as recovery:
        releases = []
        with pytest.raises(ValueError, match="public"):
            invoke(tmp_path, public_source_provider="wwr",
                   public_source_preflight=lambda stage, context: releases.append(stage))
    assert releases == []
    recapture.assert_not_called()
    recovery.assert_not_called()
    saved = json.loads((tmp_path / "journey.json").read_text())
    assert saved["segments"]["public"]["status"] == "FAIL"


def test_public_fixture_or_selected_mock_job_cannot_earn_real_source_credit():
    for field, value in (("execution_scope", "CONTROLLED_TRANSPORT"),
                         ("observed_real_job_ids", ["mock-job-1"]),
                         ("profile_id", "other-profile")):
        artifact = public_artifact()
        artifact[field] = value
        with pytest.raises(ValueError, match="public"):
            journey.validate_public_artifact(artifact, baseline(), "wwr", RUN_ID)


def test_recaptured_baseline_allows_only_catalog_and_inventory_changes():
    original = baseline()
    updated = copy.deepcopy(original)
    updated["job_inventory"] = inventory(("mock-job-1", "real-job-1"))
    updated["local_persistence"]["domains"]["company_source_catalog"] = {"count": 2, "sha256": "b" * 64}
    assert journey._validate_recaptured_baseline(original, updated) == updated
    for domain in journey.persistence.DOMAINS:
        if domain == "company_source_catalog":
            continue
        changed = copy.deepcopy(updated)
        changed["local_persistence"]["domains"][domain]["sha256"] = "c" * 64
        with pytest.raises(ValueError, match="protected persistence"):
            journey._validate_recaptured_baseline(original, changed)
    changed = copy.deepcopy(updated)
    changed["confirmation"]["receipt"] = "another-receipt"
    with pytest.raises(ValueError, match="protected UI baseline"):
        journey._validate_recaptured_baseline(original, changed)


def scoped_observation(step):
    row = observation(step)
    row["checks"] = [{"name": name, "passed": True} for name in sorted(journey.REQUIRED_CHECKS[step])]
    row["continuity"] = {"profile_id": IDENTITY["profile_id"],
                         "job_id": "browse-job-1" if step < 32 else "mock-job-1"}
    if step >= 33:
        row["continuity"]["application_id"] = IDENTITY["application_id"]
    if step == 33:
        row["continuity"]["document_ids"] = ["resume-1", "letter-1"]
    elif step == 34:
        row["continuity"]["document_ids"] = ["base-1", "resume-1"]
    elif step == 35:
        row["continuity"]["document_id"] = "letter-1"
    return row


@pytest.mark.parametrize("step", [17, 18, 19, 20, 21, 22, 32, 33, 34, 35])
def test_pass_requires_concrete_job_and_document_lineage(step):
    artifact = baseline()
    artifact["acceptance_observations"] = [scoped_observation(step)]
    rows = journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID,
                                        source=SOURCE, identity=journey._identity(artifact))
    assert rows[0]["continuity"]["job_id"] == ("browse-job-1" if step < 32 else "mock-job-1")
    for field in ("job_id", *( ["application_id"] if step >= 33 else []),
                  *( ["document_ids"] if step in {33, 34} else []),
                  *( ["document_id"] if step == 35 else [])):
        missing = copy.deepcopy(artifact)
        missing["acceptance_observations"][0]["continuity"].pop(field)
        with pytest.raises(ValueError, match="continuity"):
            journey.numbered_observations(missing, segment="ui", run_id=RUN_ID,
                                          source=SOURCE, identity=journey._identity(missing))


@pytest.mark.parametrize("step,changes", [
    (18, {"job_id": "another-profile-job"}),
    (18, {"application_id": IDENTITY["application_id"]}),
    (32, {"job_id": "browse-job-1"}),
    (33, {"job_id": "browse-job-1"}),
    (33, {"application_id": "other-application"}),
    (33, {"document_ids": ["other-application-document"]}),
    (34, {"document_ids": ["resume-1"]}),
    (34, {"document_ids": ["resume-1", "resume-1"]}),
    (34, {"document_ids": "resume-1"}),
    (35, {"document_id": "other-application-letter"}),
])
def test_mismatched_job_or_document_lineage_is_rejected(step, changes):
    artifact = baseline()
    artifact["acceptance_observations"] = [scoped_observation(step)]
    artifact["acceptance_observations"][0]["continuity"].update(changes)
    with pytest.raises(ValueError, match="continuity"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID,
                                      source=SOURCE, identity=journey._identity(artifact))


@pytest.mark.parametrize("field,value", [
    ("observed_job_ids", ["https://example.com/private"]),
    ("observed_job_ids", ["job-1", "job-1"]),
    ("selected_application_job_id", "https://example.com/private"),
    ("application_document_ids", []),
    ("application_document_ids", ["document-1"] * 101),
    ("selected_application_id", "another-application"),
])
def test_authoritative_lineage_readback_ids_are_bounded_and_consistent(field, value):
    artifact = baseline()
    artifact[field] = value
    with pytest.raises(ValueError, match="identity"):
        journey._identity(artifact)


def test_lineage_authority_is_required_and_public_recapture_cannot_retarget_application():
    artifact = baseline()
    artifact["acceptance_observations"] = [scoped_observation(33)]
    for field in ("selected_application_job_id", "application_document_ids"):
        missing = copy.deepcopy(artifact)
        missing.pop(field)
        with pytest.raises(ValueError, match="continuity"):
            journey.numbered_observations(missing, segment="ui", run_id=RUN_ID,
                                          source=SOURCE, identity=journey._identity(missing))
    updated = copy.deepcopy(artifact)
    updated["selected_application_job_id"] = "different-job"
    with pytest.raises(ValueError, match="selected application"):
        journey._validate_recaptured_baseline(artifact, updated)


def test_report_keeps_browsed_job_scope_separate_from_selected_application_scope(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    checkpoint.binding["frontend_build"] = {"status": "PASS"}
    artifact = baseline()
    artifact["acceptance_observations"] = [scoped_observation(18), scoped_observation(33)]
    checkpoint.complete_segment("ui", artifact, "a" * 64)
    report = checkpoint.payload()
    assert report["steps"][17]["verification"]["job_scope"] == "SAME_PROFILE_BROWSED_JOB"
    assert report["steps"][32]["verification"]["job_scope"] == "SELECTED_APPLICATION_JOB"
    assert report["steps"][17]["verification"]["continuity"]["job_id"] == "browse-job-1"
    assert report["steps"][32]["verification"]["continuity"]["job_id"] == "mock-job-1"
    assert report["steps"][32]["verification"]["continuity"]["document_ids"] == ["resume-1", "letter-1"]


def complete_recovery_payload():
    value = recovered()
    value["compose_target"]["services"] = list(journey.recovery.RECOVERY_RESTART_SERVICES)
    health = {name: {"status": "running", "health": "healthy"} for name in journey.recovery.RECOVERY_RESTART_SERVICES}
    value["container_health"] = {"before": health, "after": copy.deepcopy(health)}
    value["local_persistence"] = {"result": "PASS", "domains": list(journey.persistence.DOMAINS)}
    value["public_job_inventory_recovery"] = {"result": "PASS", **{
        key: inventory()[key] for key in ("profile_id", "job_count", "source_copy_count", "sha256")}}
    return value


def recovery_runtime(started):
    return {**RUNTIME, "service_started_at": {name: started for name in journey.recovery.RECOVERY_RESTART_SERVICES}}


def test_recovery_credit_requires_real_restart_markers_and_specific_readbacks(tmp_path):
    raw = complete_recovery_payload()
    before = recovery_runtime("2000-01-01T00:00:00+00:00")
    after = recovery_runtime("2001-01-01T00:00:00+00:00")
    state = baseline()
    with patch.object(journey, "observe_recovery_readbacks", return_value={
            "job_inventory": state["job_inventory"], "local_persistence": state["local_persistence"],
            "application_recovered": True}):
        bound = bind_test_recovery(tmp_path, state, raw, before, after)
    assert bound["recovery_artifact_sha256"] == hashlib.sha256((tmp_path / "compose-recovery.json").read_bytes()).hexdigest()
    assert bound["completed_at_utc"] == raw["timestamp"]
    assert bound["source"] == SOURCE
    assert all(bound[key] == IDENTITY[key] for key in IDENTITY)
    assert {row["step"] for row in bound["acceptance_observations"] if row["status"] == "PASS"} == set(range(54, 59))


def test_legacy_recovery_overall_pass_does_not_fabricate_54_to_58(tmp_path):
    with patch.object(journey, "observe_recovery_readbacks") as observe:
        bound = bind_test_recovery(tmp_path, baseline(), recovered(), RUNTIME, RUNTIME)
    observe.assert_not_called()
    assert {row["status"] for row in bound["acceptance_observations"]} == {"NOT_RUN"}


def test_step55_nested_not_run_is_not_promoted_by_persistence_pass(tmp_path):
    raw = complete_recovery_payload()
    raw["public_job_inventory_recovery"] = {"result": "NOT_RUN"}
    state = baseline()
    with patch.object(journey, "observe_recovery_readbacks", return_value={
            "job_inventory": state["job_inventory"], "local_persistence": state["local_persistence"],
            "application_recovered": True}):
        bound = bind_test_recovery(tmp_path, state, raw,
            recovery_runtime("2000-01-01T00:00:00+00:00"), recovery_runtime("2001-01-01T00:00:00+00:00"))
    by_step = {row["step"]: row for row in bound["acceptance_observations"]}
    assert by_step[55]["status"] == "NOT_RUN"
    assert by_step[56]["status"] == by_step[57]["status"] == by_step[58]["status"] == "PASS"


def test_restart_marker_or_fresh_inventory_drift_cannot_earn_pass(tmp_path):
    raw = complete_recovery_payload()
    state = baseline()
    readbacks = {"job_inventory": state["job_inventory"], "local_persistence": state["local_persistence"],
                 "application_recovered": True}
    runtime = recovery_runtime("2000-01-01T00:00:00+00:00")
    with patch.object(journey, "observe_recovery_readbacks", return_value=readbacks):
        bound = bind_test_recovery(tmp_path, state, raw, runtime, runtime)
    assert next(row for row in bound["acceptance_observations"] if row["step"] == 54)["status"] != "PASS"
    assert not any(row["status"] == "PASS" for row in bound["acceptance_observations"])
    changed = {**readbacks, "job_inventory": inventory(("different-job",))}
    with patch.object(journey, "observe_recovery_readbacks", return_value=changed), pytest.raises(ValueError):
        bind_test_recovery(tmp_path, state, raw, runtime, runtime)


def bind_test_recovery(tmp_path, state, raw, before, after):
    path = tmp_path / "compose-recovery.json"
    emit(path, raw)
    return journey.bind_recovery_evidence(state, path, RUN_ID, TARGET, before, after)


def monitor_preparation(state):
    now = journey.datetime.now(timezone.utc)
    binding = {"profile_id": state["profile_id"], "source_id": "company-source-1",
               "source_version": "a" * 64, "revision": 1, "enabled": True,
               "poll_interval": 300, "external_submission_enabled": False,
               "operation_id": "monitor-before", "completed_operation_id": "monitor-before",
               "next_due": (now + timedelta(seconds=300)).isoformat()}
    operation = {"id": "monitor-before", "state": "COMPLETED", "attempt_count": 1,
                 "started_at": now.isoformat(), "completed_at": now.isoformat(),
                 "params": {"fixture_source_id": "company-source-1", "profile_id": state["profile_id"]}}
    return {**IDENTITY, "source": SOURCE, "acceptance_run_id": RUN_ID, "result": "PARTIAL",
            "stage": "prepare", "stage_result": "PASS", "timestamp": now.isoformat(),
            "baseline_sha256": journey.digest(state), "external_submission_enabled": False,
            "monitoring_binding": binding, "monitoring_context": {"source_id": "company-source-1"},
            "refresh_operation": operation,
            "refresh_receipt": {"kind": "PUBLIC_COMPLETED_OPERATION_POINTER", "profile_id": state["profile_id"],
                                "source_id": "company-source-1", "operation_id": "monitor-before"},
            "job_inventory": state["job_inventory"], "local_persistence": state["local_persistence"], "observations": []}


def monitor_after_artifact(state, prep, receipt):
    value = copy.deepcopy(prep)
    value.update(stage="after_restart", timestamp=journey.utc_now(), preparation_sha256=journey.digest(prep),
                 recovery_evidence_sha256=journey.digest(receipt))
    value["monitoring_binding"].update(operation_id="monitor-after", completed_operation_id="monitor-after",
                                      next_due=(journey.datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat())
    value["refresh_operation"].update(id="monitor-after", started_at=journey.utc_now(), completed_at=journey.utc_now())
    value["refresh_operation"]["params"].update(predecessor_operation_id="monitor-before", scheduled_for=prep["monitoring_binding"]["next_due"])
    value["refresh_receipt"]["operation_id"] = "monitor-after"
    value["observations"] = [{"step": step, "status": "PASS", "observable_result": "Bound natural scheduled operation observed",
        "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": SOURCE["commit"], "run_id": RUN_ID,
        "continuity": dict(IDENTITY), "checks": [{"name": name, "passed": True} for name in sorted(journey.REQUIRED_CHECKS[step])]}
        for step in (59, 60)]
    return value


def test_monitoring_is_opt_in_and_hooks_bracket_actual_recovery(tmp_path):
    clock = [datetime.now(timezone.utc)]
    initial_time = clock[0]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    events = []
    retained = {}
    def prepare(url, state, path, **kwargs):
        events.append("prepare")
        retained["baseline"] = copy.deepcopy(state)
        retained["preparation"] = monitor_preparation(state)
        emit(path, retained["preparation"])
    def raw_recovery(path, output, *args):
        events.append("recovery")
        candidate = json.loads(Path(path).read_text())
        assert "monitoring_preparation" in candidate
        assert candidate["monitoring_preparation"]["artifact_sha256"] == hashlib.sha256((tmp_path / "monitor_prepare.json").read_bytes()).hexdigest()
        clock[0] += timedelta(seconds=10)
        emit(output, complete_recovery_payload())
    def after(url, state, preparation, path, **kwargs):
        events.append("after")
        assert state == retained["baseline"] and preparation == retained["preparation"]
        receipt = kwargs["recovery_evidence"]
        assert receipt["recovery_artifact_sha256"] == hashlib.sha256(Path(receipt["recovery_artifact_path"]).read_bytes()).hexdigest()
        clock[0] = journey._instant(preparation["monitoring_binding"]["next_due"]) + timedelta(seconds=1)
        emit(path, monitor_after_artifact(state, preparation, receipt))
    def runtime(*_):
        stamp = initial_time - timedelta(seconds=10) if "recovery" not in events else initial_time + timedelta(seconds=1)
        return recovery_runtime(stamp.isoformat())
    with patch.object(journey, "datetime", Clock), patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", side_effect=runtime), \
            patch.object(journey, "_monitoring_module", return_value=SimpleNamespace(prepare=prepare, after_restart=after)), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())), \
            patch.object(journey.recovery, "run", side_effect=raw_recovery), \
            patch.object(journey, "observe_recovery_readbacks", side_effect=lambda _, state: {
                "job_inventory": state["job_inventory"], "local_persistence": state["local_persistence"], "application_recovered": True}):
        report = invoke(tmp_path, monitoring=True)
        restored = journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, monitoring=True).payload()
        assert restored["steps"][59]["verification"]["resume_imported"] is True
        assert restored["steps"][59]["verification"]["executed_at_utc"] == report["steps"][59]["verification"]["executed_at_utc"]
        with (tmp_path / "monitor_prepare.json").open("ab") as stream:
            stream.write(b" ")
        with pytest.raises(ValueError, match="lineage"):
            journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, monitoring=True)
    assert events == ["prepare", "recovery", "after"]
    assert all(report["steps"][step - 1]["status"] == "PASS" for step in range(54, 61))
    assert report["result"] == "PARTIAL"
    assert json.loads((tmp_path / "ui.json").read_text()) == retained["baseline"]


def test_monitoring_prepare_failure_stops_before_recovery(tmp_path):
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey, "_monitoring_module", return_value=SimpleNamespace(
                prepare=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("controlled failure")))), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())), \
            patch.object(journey.recovery, "run") as recovery:
        with pytest.raises(RuntimeError):
            invoke(tmp_path, monitoring=True)
    recovery.assert_not_called()
    assert json.loads((tmp_path / "journey.json").read_text())["segments"]["monitor_prepare"]["status"] == "FAIL"


def application_stage_artifact(step=44):
    artifact = baseline()
    row = observation(step)
    row["checks"] = [{"name": name, "passed": True} for name in sorted(journey.REQUIRED_CHECKS[step])]
    row["continuity"] = {"profile_id": IDENTITY["profile_id"], "application_id": IDENTITY["application_id"], "job_id": "mock-job-1"}
    if step in {41, 42}:
        row["continuity"]["document_id"] = "resume-1" if step == 41 else "letter-1"
    artifact["acceptance_observations"] = [row]
    artifact["runtime_identity"] = {key: RUNTIME[key] for key in ("source_manifest_sha256", "images")}
    artifact["stage_lineage"] = [{"step": step, "operation_kind": "APPLY_MOCK", "operation_id": "paused-operation",
        "request_id": "paused-request", "profile_id": IDENTITY["profile_id"],
        "application_id": IDENTITY["application_id"], "job_id": "mock-job-1"}]
    return artifact


def test_stage_lineage_preserves_earlier_operation_without_replacing_final_submit(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    artifact = application_stage_artifact()
    checkpoint.binding.update(runtime=RUNTIME, frontend_build={"status": "PASS"})
    checkpoint.complete_segment("ui", artifact, "a" * 64)
    report = checkpoint.payload()
    assert report["continuity"]["operation_id"] == IDENTITY["operation_id"]
    assert report["stage_lineage"][0]["operation_id"] == "paused-operation"
    assert report["steps"][43]["verification"]["stage_lineage"] == artifact["stage_lineage"][0]
    assert report["steps"][43]["status"] == "PASS"


@pytest.mark.parametrize("field,value", [("application_id", "other-app"), ("profile_id", "other-profile"),
    ("job_id", "browse-job-1"), ("operation_id", "https://private"), ("operation_kind", "PREPARE"), ("step", True)])
def test_stage_lineage_rejects_scope_and_operation_drift(field, value):
    artifact = application_stage_artifact()
    artifact["stage_lineage"][0][field] = value
    with pytest.raises(ValueError, match="lineage"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))


@pytest.mark.parametrize("step", range(36, 54))
def test_application_stages_require_actual_stage_lineage(step):
    artifact = application_stage_artifact(step)
    artifact.pop("stage_lineage")
    with pytest.raises(ValueError, match="lineage"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))


@pytest.mark.parametrize("step", [41, 42])
def test_upload_observations_require_document_identity(step):
    artifact = application_stage_artifact(step)
    artifact["acceptance_observations"][0]["continuity"].pop("document_id")
    with pytest.raises(ValueError, match="continuity"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))


def test_application_credit_requires_runtime_identity_and_final_lineage_matches_submit(tmp_path):
    artifact = application_stage_artifact()
    artifact.pop("runtime_identity")
    with pytest.raises(ValueError, match="runtime"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))
    artifact = application_stage_artifact(49)
    with pytest.raises(ValueError, match="lineage"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))
    artifact = application_stage_artifact()
    artifact["runtime_identity"] = {**artifact["runtime_identity"], "images": {"api": "sha256:" + "0" * 64}}
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    checkpoint.binding["runtime"] = RUNTIME
    artifact["timestamp"] = journey.utc_now()
    with pytest.raises(ValueError, match="runtime"):
        checkpoint.complete_segment("ui", artifact, "a" * 64)


def test_runtime_identity_is_passed_only_to_compatible_ui_producer():
    captured = {}
    def producer(*args, acceptance_run_id=None, runtime_identity=None):
        captured.update(run_id=acceptance_run_id, runtime_identity=runtime_identity)
    runtime = {key: RUNTIME[key] for key in ("source_manifest_sha256", "images")}
    journey._invoke(producer, (), RUN_ID, runtime_identity=runtime)
    assert captured == {"run_id": RUN_ID, "runtime_identity": runtime}
    journey._invoke(lambda: None, (), RUN_ID, runtime_identity=runtime)


@pytest.mark.parametrize("mutation", ["run", "source", "baseline", "preparation", "receipt", "early", "same-operation", "missed-due", "documents"])
def test_monitoring_rejects_stale_or_mismatched_proof(mutation):
    state = baseline()
    prep = monitor_preparation(state)
    prep["monitoring_binding"]["next_due"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    prep["timestamp"] = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    receipt = {"completed_at_utc": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()}
    after = monitor_after_artifact(state, prep, receipt)
    if mutation == "run": after["acceptance_run_id"] = "other-run"
    if mutation == "source": after["source"] = {**SOURCE, "commit": "c" * 40}
    if mutation == "baseline": after["baseline_sha256"] = "0" * 64
    if mutation == "preparation": after["preparation_sha256"] = "0" * 64
    if mutation == "receipt": after["recovery_evidence_sha256"] = "0" * 64
    if mutation == "early": after["refresh_operation"]["started_at"] = receipt["completed_at_utc"]
    if mutation == "same-operation": after["refresh_operation"]["id"] = prep["refresh_operation"]["id"]
    if mutation == "missed-due": receipt["completed_at_utc"] = journey.utc_now(); after["recovery_evidence_sha256"] = journey.digest(receipt)
    if mutation == "documents": after["local_persistence"] = copy.deepcopy(after["local_persistence"]); after["local_persistence"]["domains"]["cover_letters"]["sha256"] = "b" * 64
    with pytest.raises(ValueError):
        journey.validate_monitoring_result("monitor_after", after, state, RUN_ID, preparation=prep, recovery_evidence=receipt)


def test_bound_recovery_rejects_modified_actual_raw_bytes(tmp_path):
    state = baseline()
    bound = bind_test_recovery(tmp_path, state, recovered(), RUNTIME, RUNTIME)
    assert journey._validate_bound_recovery(bound, state, tmp_path) == bound
    with (tmp_path / "compose-recovery.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="raw artifact"):
        journey._validate_bound_recovery(bound, state, tmp_path)


def test_default_run_never_loads_monitoring_and_requires_explicit_boolean(tmp_path):
    with patch.object(journey, "_monitoring_module") as module, \
            patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())), \
            patch.object(journey.recovery, "run", side_effect=lambda _, path, *args: emit(path, recovered())):
        report = invoke(tmp_path)
        module.assert_not_called()
        assert set(report["segments"]) == {"ui", "recovery"}
        with pytest.raises(ValueError, match="boolean"):
            invoke(tmp_path, monitoring="true")


def startup_receipt_and_runtime():
    services = journey.recovery.RECOVERY_RESTART_SERVICES
    created = "2026-11-06T00:00:01+00:00"
    started = "2026-11-06T00:00:02+00:00"
    runtime = {**RUNTIME, "images": {name: "sha256:" + "e" * 64 for name in services},
        "containers": {name: name + "-container" for name in services},
        "service_started_at": {name: started for name in services},
        "container_created_at": {name: created for name in services},
        "health": {name: {"status": "running", "health": "healthy"} for name in services},
        "migration": {"container_id": "migrate-container", "status": "exited", "exit_code": 0,
            "created_at": created, "finished_at_utc": started, "image_id": "sha256:" + "e" * 64}}
    receipt = {"schema_version": 1, "result": "PASS", "source": SOURCE, "acceptance_run_id": RUN_ID,
        "target": TARGET, "started_at_utc": "2026-11-06T00:00:00+00:00",
        "completed_at_utc": "2026-11-06T00:00:03+00:00", "preexisting_container_ids": [], "preexisting_volume_ids": [],
        "command": ["docker", "compose", "--project-name", TARGET["project_name"], "-f", "compose.yml", "up", "-d", "--no-build"],
        "command_exit_code": 0, **{key: copy.deepcopy(runtime[key]) for key in
            ("images", "containers", "service_started_at", "container_created_at", "health", "migration")}}
    return receipt, runtime


def test_startup_requires_actual_initial_creation_migration_and_ready_stack():
    receipt, runtime = startup_receipt_and_runtime()
    row = journey.validate_startup_receipt(receipt, SOURCE, RUN_ID, TARGET, runtime)
    assert row["step"] == 1 and row["status"] == "PASS"
    assert {check["name"] for check in row["checks"]} == journey.REQUIRED_CHECKS[1]


@pytest.mark.parametrize("change", ["prior-container", "prior-volume", "old-created", "restarted", "migration", "image", "run", "target", "source", "command", "compose-files", "duplicate-project", "schema"])
def test_startup_rejects_reused_or_unready_runtime(change):
    receipt, runtime = startup_receipt_and_runtime()
    if change == "prior-container": receipt["preexisting_container_ids"] = ["already-there"]
    if change == "prior-volume": receipt["preexisting_volume_ids"] = ["already-there"]
    if change == "old-created": runtime["container_created_at"]["api"] = "2026-11-05T00:00:00+00:00"
    if change == "restarted": runtime["service_started_at"]["api"] = journey.utc_now()
    if change == "migration": runtime["migration"]["exit_code"] = 1
    if change == "image": runtime["images"]["api"] = "sha256:" + "0" * 64
    if change == "run": receipt["acceptance_run_id"] = "old-run"
    if change == "target": receipt["target"] = {**TARGET, "port": 18888}
    if change == "source": receipt["source"] = {**SOURCE, "commit": "f" * 40}
    if change == "command": receipt["command"] = ["docker", "compose", "restart"]
    if change == "compose-files": receipt["command"][6] = "other.yml"
    if change == "duplicate-project": receipt["command"][2:2] = ["-p", "mjobs-fixture-aaaaaaaaaaaa"]
    if change == "schema": receipt["schema_version"] = True
    with pytest.raises(ValueError):
        journey.validate_startup_receipt(receipt, SOURCE, RUN_ID, TARGET, runtime)


def test_startup_receipt_is_optional_and_preserved_as_historical_proof_on_resume(tmp_path):
    receipt, runtime = startup_receipt_and_runtime()
    input_path = tmp_path / "actual-startup.json"
    emit(input_path, receipt)
    directory = tmp_path / "evidence"
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=runtime), \
            patch.object(journey, "observe_startup_runtime", return_value=runtime) as observe, \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())):
        report = invoke(directory, startup_receipt=input_path, stop_after_ui=True)
        assert report["steps"][0]["status"] == "PASS"
        assert report["steps"][0]["verification"]["executed_at_utc"] == receipt["completed_at_utc"]
        resumed = invoke(directory, resume=True, stop_after_ui=True)
        assert observe.call_count == 1
        assert resumed["steps"][0]["verification"]["resume_imported"] is True
        assert (directory / "startup-receipt.json").read_bytes() == input_path.read_bytes()
        with (directory / "startup-receipt.json").open("ab") as stream:
            stream.write(b" ")
        with pytest.raises(ValueError, match="Startup evidence lineage"):
            invoke(directory, resume=True, stop_after_ui=True)


def test_ui_cannot_claim_startup_from_its_numbered_boolean():
    artifact = baseline()
    receipt, runtime = startup_receipt_and_runtime()
    artifact["acceptance_observations"] = [journey.validate_startup_receipt(receipt, SOURCE, RUN_ID, TARGET, runtime)]
    with pytest.raises(ValueError, match="out-of-segment"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))


def public_browser_artifact(state, public):
    runtime = {key: RUNTIME[key] for key in ("source_manifest_sha256", "images", "dockerfile_sha256", "frontend_assets")}
    value = {key: public.get(key) for key in (*IDENTITY, "source", "acceptance_run_id", "selected_application_job_id", "observed_real_job_ids",
        "collection_operation_id", "collection_request_id", "source_id", "provider", "execution_scope")}
    value.update(schema_version=1, result="PASS", evidence_kind="PUBLIC_BROWSER_READ_ONLY", timestamp_utc=journey.utc_now(),
        runtime_identity=runtime, runtime_identity_sha256=journey.digest(runtime),
        browser={"loopback_only": True, "get_head_only": True, "external_requests": 0, "page_errors": 0,
                 "blocked_write_attempts": 0, "blocked_write_paths": []},
        baseline_readback={"unchanged": True, "before_sha256": "a" * 64, "after_sha256": "a" * 64, "profile_version": public["profile_version"],
            "selected_application_job_id": public["selected_application_job_id"],
            "selected_application_route_id": public["selected_application_route_id"], "document_ids": state["application_document_ids"]},
        acceptance_observations=[{"step": 11, "status": "PASS", "checks": {"public_jobs_visible_in_same_profile_ui": True}},
                                 {"step": 16, "status": "PASS", "checks": {"source_remote_restriction_visible": True}}])
    value["sha256"] = journey.digest(value)
    return value


def test_public_browser_join_requires_both_provenance_and_visible_checks():
    state, public = baseline(), public_artifact()
    browser = public_browser_artifact(state, public)
    joined = journey.join_public_browser(state, public, browser, "a" * 64, "b" * 64, RUNTIME)
    rows = journey.numbered_observations(joined, segment="public_browser", run_id=RUN_ID, source=SOURCE, identity=journey._identity(state))
    assert [(row["step"], row["status"]) for row in rows] == [(11, "PASS"), (16, "PASS")]
    assert {row["name"] for row in rows[0]["checks"]} == journey.REQUIRED_CHECKS[11]
    browser["acceptance_observations"][0].update(status="PARTIAL", checks={"public_jobs_visible_in_same_profile_ui": False})
    browser["sha256"] = journey.digest({key: value for key, value in browser.items() if key != "sha256"})
    assert journey.join_public_browser(state, public, browser, "a" * 64, "b" * 64, RUNTIME)["acceptance_observations"][0]["status"] == "PARTIAL"


@pytest.mark.parametrize("change", ["run", "source", "jobs", "runtime", "documents", "mutated", "egress", "stale", "fabricated-pass"])
def test_public_browser_join_rejects_mismatched_or_missing_observation(change):
    state, public = baseline(), public_artifact()
    browser = public_browser_artifact(state, public)
    if change == "run": browser["acceptance_run_id"] = "other-run"
    if change == "source": browser["source"] = {**SOURCE, "commit": "b" * 40}
    if change == "jobs": browser["observed_real_job_ids"] = ["other-job"]
    if change == "runtime": browser["runtime_identity"] = {}
    if change == "documents": browser["baseline_readback"]["document_ids"] = ["other-doc"]
    if change == "mutated": browser["baseline_readback"]["after_sha256"] = "c" * 64
    if change == "egress": browser["browser"]["external_requests"] = 1
    if change == "stale": browser["timestamp_utc"] = "2000-01-01T00:00:00Z"
    if change == "fabricated-pass": browser["acceptance_observations"][0]["checks"]["public_jobs_visible_in_same_profile_ui"] = False
    browser["sha256"] = journey.digest({key: value for key, value in browser.items() if key != "sha256"})
    with pytest.raises(ValueError):
        journey.join_public_browser(state, public, browser, "a" * 64, "b" * 64, RUNTIME)


def employer_artifact(step):
    artifact = baseline()
    artifact["employer_discovery_lineage"] = {"profile_id": IDENTITY["profile_id"], "operation_id": "initial-discovery",
        "request_id": "initial-discovery-request", "job_ids": ["browse-job-1", "mock-job-1"]}
    artifact["acceptance_observations"] = [{"step": step, "status": "PASS", "observable_result": "Observed same-profile employer discovery predicate",
        "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": SOURCE["commit"], "run_id": RUN_ID,
        "continuity": {"profile_id": IDENTITY["profile_id"], "job_id": "browse-job-1", "operation_id": "initial-discovery",
                       "request_id": "initial-discovery-request"},
        "checks": [{"name": name, "passed": True} for name in journey.REQUIRED_CHECKS[step]]}]
    return artifact


@pytest.mark.parametrize("step", range(23, 32))
def test_employer_discovery_uses_original_discover_identity_not_final_submit(step):
    artifact = employer_artifact(step)
    rows = journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))
    assert rows[0]["continuity"]["operation_id"] == "initial-discovery"
    assert artifact["operation_id"] == IDENTITY["operation_id"]


@pytest.mark.parametrize("change", ["missing-lineage", "final-operation", "final-request", "wrong-profile", "wrong-job", "application-scope"])
def test_employer_discovery_rejects_unrelated_stage_or_job(change):
    artifact = employer_artifact(24)
    continuity = artifact["acceptance_observations"][0]["continuity"]
    if change == "missing-lineage": artifact.pop("employer_discovery_lineage")
    if change == "final-operation": continuity["operation_id"] = IDENTITY["operation_id"]
    if change == "final-request": continuity["request_id"] = IDENTITY["request_id"]
    if change == "wrong-profile": artifact["employer_discovery_lineage"]["profile_id"] = "other-profile"
    if change == "wrong-job": artifact["employer_discovery_lineage"]["job_ids"] = ["other-job"]
    if change == "application-scope": continuity["application_id"] = IDENTITY["application_id"]
    with pytest.raises(ValueError, match="identity|continuity"):
        journey.numbered_observations(artifact, segment="ui", run_id=RUN_ID, source=SOURCE, identity=journey._identity(artifact))


@pytest.mark.parametrize("cleanup_status", ["RESTORED_DISABLED", "NOT_REQUIRED"])
def test_public_release_preserves_latched_live_callback_identity(tmp_path, cleanup_status):
    latched = {}
    events = []
    fields = ("collection_operation_id", "collection_request_id", "source_id", "source_policy_sha256", "newly_created")
    def containment(stage, context):
        if stage == "poll":
            latched.update({key: context[key] for key in fields})
            return {"scheduler_contained": True}
        assert stage == "release"
        assert latched and {key: context.get(key) for key in fields} == latched
        assert context["cleanup_status"] == cleanup_status
        events.append("release")
        return {"released": True, "scheduler_contained": False}
    def public_run(url, original, path, **kwargs):
        value = public_artifact()
        value["cleanup"]["status"] = cleanup_status
        # This is the exact post-POST callback context emitted by the LIVE
        # helper's own HTTP transport, without injecting a request proxy.
        assert "request" not in kwargs
        kwargs["containment_preflight"]("poll", {
            **{key: value[key] for key in fields[:-1]}, "newly_created": cleanup_status == "RESTORED_DISABLED"})
        emit(path, value)
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey, "_public_source_module", return_value=SimpleNamespace(run=public_run)), \
            patch.object(journey, "recapture_public_baseline", side_effect=lambda original, *_: {
                **original, "job_inventory": inventory(("mock-job-1", "real-job-1"))}), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())), \
            patch.object(journey.recovery, "run", side_effect=lambda _, path, *args: (events.append("recovery"), emit(path, recovered()))):
        report = invoke(tmp_path, public_source_provider="wwr", public_source_preflight=containment)
    assert events == ["release", "recovery"]
    assert report["segments"]["public"]["status"] == "COMPLETE"


@pytest.mark.parametrize("policy_hash", [None, "not-a-policy-hash"])
def test_public_release_cannot_use_missing_or_malformed_policy_identity(policy_hash):
    artifact = public_artifact()
    artifact["source_policy_sha256"] = policy_hash
    with pytest.raises(ValueError, match="policy identity"):
        journey.validate_public_artifact(artifact, baseline(), "wwr", RUN_ID)


def source_setup_artifact(state, public):
    policy = {"provider": "remotive", "tenant": "default", "enabled": True, "poll_interval": 604800,
        "concurrency": 1, "request_timeout": 10, "rate_limit": 1, "jitter_seconds": 0}
    projection = {**policy, "id": "extra-remotive", "strategy": "HTTP", "capabilities": ["DISCOVER"]}
    artifact = {"schema_version": 2, "result": "PASS", "timestamp": journey.utc_now(), "source": SOURCE,
        "acceptance_run_id": RUN_ID, "target_origin": TARGET["base_url"], "profile_id": state["profile_id"],
        "application_id": state["application_id"], "job_id": state["selected_application_job_id"],
        "runtime_identity": {key: RUNTIME[key] for key in ("source_manifest_sha256", "images")},
        "primary": {"provider": "wwr", "source_id": public["source_id"], "policy_sha256": public["source_policy_sha256"]},
        "owned_extra": {"provider": "remotive", "source_id": "extra-remotive", "enabled_policy_sha256": journey.digest(projection),
                        "disabled_policy_sha256": journey.digest({**projection, "enabled": False}), "disabled": True},
        "extra_collection_operation_ids": [],
        "extra_operation_readback_sha256": journey.digest([]),
        "acceptance_observations": [{"step": 9, "status": "PASS", "observable_result": "Both configured sources visible and enabled before owned extra was disabled",
            "evidence_kind": "PUBLIC_READ_ONLY", "run_id": RUN_ID, "source_commit": SOURCE["commit"],
            "continuity": {"profile_id": state["profile_id"]},
            "checks": [{"name": name, "passed": True} for name in sorted(journey.REQUIRED_CHECKS[9])]}]}
    extra, primary = artifact["owned_extra"], artifact["primary"]
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    binding = {"acceptance_run_id": RUN_ID, "target_origin": TARGET["base_url"],
        "profile_id": state["profile_id"], "application_id": state["application_id"],
        "job_id": state["selected_application_job_id"], "primary_provider": "wwr",
        "primary_source_id": primary["source_id"], "primary_policy_sha256": primary["policy_sha256"],
        "extra_source_id": extra["source_id"], "extra_enabled_policy_sha256": extra["enabled_policy_sha256"],
        "extra_disabled_policy_sha256": extra["disabled_policy_sha256"], "extra_enabled": False}
    flags = {key: True for key in ("scheduler_contained", "source_policy_exclusive", "source_collection_exclusive")}
    release = {**{key: False for key in flags}, "additional_source_exclusive": False, "released": True,
        "project": TARGET["project_name"], "scheduler_container_id": RUNTIME["containers"]["scheduler"],
        "lease_expired_at": artifact["timestamp"]}
    primary_checks = []
    for stage in ("preflight", "enable", "dispatch", "poll", "read", "finalize", "cleanup", "finalize", "release"):
        row = {**binding, "stage": stage, "verified": True, **flags,
            "owner": "fixture-owner", "token_sha256": "7" * 64, "lease_expires_at": expiry,
            "checked_at": artifact["timestamp"], "receipt": {**flags, "lease_expires_at": expiry}}
        if stage == "preflight":
            row.update(primary_source_id=None, primary_policy_sha256=None,
                extra_source_id=None, extra_enabled_policy_sha256=None, extra_disabled_policy_sha256=None)
        if stage == "release":
            row.update(**{key: False for key in flags}, owner=None, token_sha256=None,
                       lease_expires_at=None, receipt=release)
        primary_checks.append(row)
    owner_checks = [{"stage": "bind_absent", "verified": True, "verification_scope": "LIVE_OWNER_API",
        "provider": "remotive", "tenant": "default", "source_id": None,
        "enabled_policy_sha256": None, "disabled_policy_sha256": None, "enabled": False,
        "lease_expires_at": expiry, "lease_model": "LOCAL_OPERATOR_CLAIM_NO_SERVER_SIDE_CAS",
        "receipt": {"additional_source_absent": True, "provider": "remotive", "tenant": "default",
            "lease_expires_at": expiry, "lease_model": "LOCAL_OPERATOR_CLAIM_NO_SERVER_SIDE_CAS"}}]
    for stage, enabled in (("created", True), ("check", True), ("before_disable", True),
                           ("disabled", False), ("check", False), ("release", False)):
        owner_checks.append({"stage": stage, "verified": True, "verification_scope": "LIVE_OWNER_API",
            "source_id": extra["source_id"], "enabled_policy_sha256": extra["enabled_policy_sha256"],
            "disabled_policy_sha256": None if enabled else extra["disabled_policy_sha256"],
            "enabled": enabled, "lease_expires_at": expiry,
            "receipt": {"additional_source_policy_verified": True, "additional_source_id": extra["source_id"],
                "additional_source_enabled": enabled, "lease_expires_at": expiry}})
    artifact.update(trust_boundary={"target": "ISOLATED_LOCAL_FIXTURE",
        "source_policy_ownership": "OPERATOR_COOPERATIVE_EXCLUSIVE", "source_policy_atomic_cas": False},
        extra_policy_readbacks=[{"stage": stage, "provider": "remotive", "tenant": "default", "present": False,
                                 "expected_policy": policy, "policy": None}
            for stage in ("preflight_absent", "immediately_before_create")] + [
                {"stage": stage, "source_id": extra["source_id"], "enabled": enabled,
                 "expected_policy": policy, "policy": {**projection, "enabled": enabled},
                 "policy_sha256": extra["enabled_policy_sha256" if enabled else "disabled_policy_sha256"]}
                for stage, enabled in (("created_enabled", True), ("before_disable", True),
                                       ("disabled_readback", False), ("release", False))],
        extra_operation_readbacks=[{"stage": stage, "source_id": extra["source_id"], "inventory_count": 1, "matching_operation_ids": [],
            "source_policy_sha256": extra["enabled_policy_sha256" if stage == "created_before_primary_collection" else "disabled_policy_sha256"],
            "matching_ids_sha256": journey.digest([])} for stage in (
                "created_before_primary_collection", "disabled_before_primary_collection", "release")],
        additional_owner_checks=owner_checks, primary_containment_checks=primary_checks,
        release_receipt={"released": True, "receipt": release,
            "binding": {**binding, "extra_provider": "remotive", "owner": "fixture-owner",
                        "token_sha256": "7" * 64, "lease_expires_at": expiry,
                        "project": TARGET["project_name"], "scheduler_container_id": RUNTIME["containers"]["scheduler"]},
            "additional_owner_release": copy.deepcopy(owner_checks[-1])})
    return artifact


def source_setup_controlled(artifact):
    artifact["result"] = artifact["acceptance_observations"][0]["status"] = "NOT_RUN"
    for row in artifact["primary_containment_checks"]:
        row["verified"] = False
    for row in artifact["additional_owner_checks"]:
        row.update(verified=False, verification_scope="CONTROLLED_TRANSPORT")
    artifact["release_receipt"]["additional_owner_release"] = copy.deepcopy(artifact["additional_owner_checks"][-1])
    return artifact


def test_source_setup_requires_same_run_ui_source_policy_and_cleanup():
    state, public = baseline(), public_artifact()
    artifact = source_setup_artifact(state, public)
    row = journey.validate_source_setup_artifact(artifact, state, public, TARGET, RUNTIME)
    assert row["step"] == 9 and row["status"] == "PASS"


def test_source_setup_rejects_legacy_receipt_without_owned_release_history():
    state, public = baseline(), public_artifact()
    artifact = source_setup_artifact(state, public)
    artifact["schema_version"] = 1
    for key in ("trust_boundary", "extra_policy_readbacks", "extra_operation_readbacks",
                "additional_owner_checks", "primary_containment_checks", "release_receipt"):
        artifact.pop(key, None)
    with pytest.raises(ValueError):
        journey.validate_source_setup_artifact(artifact, state, public, TARGET, RUNTIME)


@pytest.mark.parametrize("change", ["schema-bool", "trust-missing", "trust-cas", "trust-owner",
    "policy-missing", "policy-order", "policy-hash", "absence", "operations-missing", "operations-unbounded",
    "operations-bool", "operations-nonempty", "operations-digest", "owner-missing", "owner-scope", "owner-order",
    "owner-policy", "owner-unverified", "owner-receipt", "primary-missing", "primary-owner", "primary-token",
    "primary-lease", "primary-disabled", "primary-stage", "primary-source", "release-missing", "release-false",
    "release-scheduler", "release-project", "release-contained", "release-extra", "release-binding", "release-raw"])
def test_source_setup_v2_requires_ordered_ownership_and_release_receipts(change):
    state, public = baseline(), public_artifact()
    artifact = source_setup_artifact(state, public)
    if change == "schema-bool": artifact["schema_version"] = True
    if change == "trust-missing": artifact.pop("trust_boundary")
    if change == "trust-cas": artifact["trust_boundary"]["source_policy_atomic_cas"] = True
    if change == "trust-owner": artifact["trust_boundary"]["source_policy_ownership"] = "ASSUMED"
    if change == "policy-missing": artifact["extra_policy_readbacks"].pop()
    if change == "policy-order": artifact["extra_policy_readbacks"].reverse()
    if change == "policy-hash": artifact["extra_policy_readbacks"][3]["policy_sha256"] = "9" * 64
    if change == "absence": artifact["extra_policy_readbacks"][0]["present"] = True
    if change == "operations-missing": artifact["extra_operation_readbacks"].pop()
    if change == "operations-unbounded": artifact["extra_operation_readbacks"][0]["inventory_count"] = 100
    if change == "operations-bool": artifact["extra_operation_readbacks"][0]["inventory_count"] = True
    if change == "operations-nonempty": artifact["extra_operation_readbacks"][1]["matching_operation_ids"] = ["extra-collection"]
    if change == "operations-digest": artifact["extra_operation_readbacks"][1]["matching_ids_sha256"] = "a" * 64
    if change == "owner-missing": artifact.pop("additional_owner_checks")
    if change == "owner-scope": artifact["additional_owner_checks"][1]["verification_scope"] = "CONTROLLED_TRANSPORT"
    if change == "owner-order": artifact["additional_owner_checks"][1]["stage"] = "disabled"
    if change == "owner-policy": artifact["additional_owner_checks"][1]["enabled_policy_sha256"] = "a" * 64
    if change == "owner-unverified": artifact["additional_owner_checks"][1]["verified"] = False
    if change == "owner-receipt": artifact["additional_owner_checks"][1]["receipt"]["additional_source_id"] = "another-source"
    if change == "primary-missing": artifact.pop("primary_containment_checks")
    if change == "primary-owner": artifact["primary_containment_checks"][1]["owner"] = "another-owner"
    if change == "primary-token": artifact["primary_containment_checks"][1]["token_sha256"] = "f" * 64
    if change == "primary-lease": artifact["primary_containment_checks"][1]["lease_expires_at"] = state["timestamp"]
    if change == "primary-disabled": artifact["primary_containment_checks"][1]["scheduler_contained"] = False
    if change == "primary-stage": artifact["primary_containment_checks"][2]["stage"] = "unknown"
    if change == "primary-source": artifact["primary_containment_checks"][-1]["primary_source_id"] = "another-source"
    if change == "release-missing": artifact.pop("release_receipt")
    if change == "release-false": artifact["release_receipt"]["released"] = False
    if change == "release-scheduler": artifact["release_receipt"]["receipt"]["scheduler_container_id"] = "another-container"
    if change == "release-project": artifact["release_receipt"]["receipt"]["project"] = "another-project"
    if change == "release-contained": artifact["release_receipt"]["receipt"]["scheduler_contained"] = True
    if change == "release-extra": artifact["release_receipt"]["additional_owner_release"]["enabled"] = True
    if change == "release-binding": artifact["release_receipt"]["binding"]["token_sha256"] = "a" * 64
    if change == "release-raw": artifact["primary_containment_checks"][-1]["receipt"] = {}
    with pytest.raises(ValueError):
        journey.validate_source_setup_artifact(artifact, state, public, TARGET, RUNTIME)


@pytest.mark.parametrize("change", ["run", "source", "profile", "application", "job", "runtime", "primary", "policy", "enabled-extra", "same-source", "missing-cleanup", "extra-operation", "operation-digest", "stale", "checks", "fixture-kind", "wrong-step"])
def test_source_setup_rejects_unbound_or_unclean_evidence(change):
    state, public = baseline(), public_artifact()
    artifact = source_setup_artifact(state, public)
    if change == "run": artifact["acceptance_run_id"] = "other-run"
    if change == "source": artifact["source"] = {**SOURCE, "commit": "b" * 40}
    if change == "profile": artifact["profile_id"] = "other-profile"
    if change == "application": artifact["application_id"] = "other-app"
    if change == "job": artifact["job_id"] = "other-job"
    if change == "runtime": artifact["runtime_identity"]["images"] = {"api": "sha256:" + "b" * 64}
    if change == "primary": artifact["primary"]["source_id"] = "other-source"
    if change == "policy": artifact["primary"]["policy_sha256"] = "6" * 64
    if change == "enabled-extra": artifact["owned_extra"]["disabled"] = False
    if change == "same-source": artifact["owned_extra"]["source_id"] = public["source_id"]
    if change == "missing-cleanup": artifact["owned_extra"].pop("disabled_policy_sha256")
    if change == "extra-operation": artifact["extra_collection_operation_ids"] = ["unapproved-collect"]
    if change == "operation-digest": artifact["extra_operation_readback_sha256"] = "a" * 64
    if change == "stale": artifact["timestamp"] = "2000-01-01T00:00:00Z"
    if change == "checks": artifact["acceptance_observations"][0]["checks"][0]["passed"] = False
    if change == "fixture-kind": artifact["acceptance_observations"][0]["evidence_kind"] = "CONTROLLED_FIXTURE"
    if change == "wrong-step": artifact["acceptance_observations"][0]["step"] = 11
    with pytest.raises(ValueError):
        journey.validate_source_setup_artifact(artifact, state, public, TARGET, RUNTIME)


@pytest.mark.parametrize("setup_present,release_fails", [(False, False), (True, False), (True, True)])
def test_optional_source_setup_joins_only_after_successful_release(tmp_path, setup_present, release_fails):
    def ui_run(url, path, **kwargs):
        value = baseline()
        value["acceptance_observations"].append({"step": 9, "status": "PARTIAL", "observable_result": "Additional source configuration not observed",
            "evidence_kind": "CONTROLLED_FIXTURE", "source_commit": SOURCE["commit"], "run_id": RUN_ID,
            "continuity": {"profile_id": IDENTITY["profile_id"]},
            "checks": [{"name": name, "passed": False} for name in sorted(journey.REQUIRED_CHECKS[9])]})
        emit(path, value)
    def release(stage, context):
        assert stage == "release"
        if setup_present:
            ui = json.loads((tmp_path / "ui.json").read_text())
            public = json.loads((tmp_path / "public.json").read_text())
            emit(tmp_path / "public-source-setup.json", source_setup_artifact(ui, public))
        if release_fails:
            raise RuntimeError("controlled release failure")
    with patch.object(journey, "current_source", return_value=SOURCE), \
            patch.object(journey, "preflight", return_value=RUNTIME), \
            patch.object(journey, "_public_source_module", return_value=SimpleNamespace(run=lambda _, state, path, **kwargs: emit(path, public_artifact()))), \
            patch.object(journey, "recapture_public_baseline", side_effect=lambda original, *_: {
                **original, "job_inventory": inventory(("mock-job-1", "real-job-1"))}), \
            patch.object(journey.ui_e2e, "run", side_effect=ui_run), \
            patch.object(journey.recovery, "run", side_effect=lambda _, path, *args: emit(path, recovered())) as recovery_run:
        if release_fails:
            with pytest.raises(RuntimeError, match="release failure"):
                invoke(tmp_path, public_source_provider="wwr", public_source_preflight=release)
            recovery_run.assert_not_called()
            report = json.loads((tmp_path / "journey.json").read_text())
            assert report["steps"][8]["status"] == "PARTIAL"
            assert "source_setup" not in report
            return
        report = invoke(tmp_path, public_source_provider="wwr", public_source_preflight=release)
    assert report["steps"][8]["status"] == ("PASS" if setup_present else "PARTIAL")
    if setup_present:
        verification = report["steps"][8]["verification"]
        assert verification["evidence_artifact"] == "public-source-setup.json"
        assert verification["evidence_sha256"] == hashlib.sha256((tmp_path / "public-source-setup.json").read_bytes()).hexdigest()
        restored = journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, "wwr").payload()
        assert restored["steps"][8]["verification"]["resume_imported"] is True
        assert restored["steps"][8]["verification"]["executed_at_utc"] == verification["executed_at_utc"]
        with (tmp_path / "public-source-setup.json").open("ab") as stream:
            stream.write(b" ")
        with pytest.raises(ValueError, match="Source setup evidence reference hash mismatch"):
            journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, "wwr")


@pytest.mark.parametrize("execution_scope,expected_status", [("LIVE_PUBLIC_READ_ONLY", "PASS"), ("CONTROLLED_TRANSPORT", "NOT_RUN")])
def test_actual_source_setup_publisher_schema_joins_without_relabeling(tmp_path, execution_scope, expected_status):
    """Use the real serializer, not a duplicate fixture envelope; no HTTP/browser execution."""
    producer = pytest.importorskip("cli.acceptance_source_setup")
    state, public = baseline(), public_artifact()
    sample = source_setup_artifact(state, public)
    if execution_scope == "CONTROLLED_TRANSPORT":
        source_setup_controlled(sample)
    path = tmp_path / "public-source-setup.json"
    # Only serializer state is seeded. This verifies the schema contract and
    # earns no browser/provider acceptance; any accidental HTTP call fails.
    def no_request(*args, **kwargs):
        raise AssertionError("Schema test must not perform HTTP requests")
    with patch.object(journey.httpx, "Client") as client:
        client.return_value.request.side_effect = no_request
        wrapped = producer.compose_owned_public_containment(lambda *_: None,
            base_url=TARGET["base_url"], baseline=state, acceptance_run_id=RUN_ID,
            artifact_path=path, runtime_identity=sample["runtime_identity"], execution_scope=execution_scope,
            **({"request": no_request} if execution_scope == "CONTROLLED_TRANSPORT" else {}))
        composed = wrapped.composed if execution_scope == "LIVE_PUBLIC_READ_ONLY" else wrapped
        composed.primary = sample["primary"]
        composed.extra_id = sample["owned_extra"]["source_id"]
        composed.enabled_policy_sha = sample["owned_extra"]["enabled_policy_sha256"]
        composed.disabled_policy_sha = sample["owned_extra"]["disabled_policy_sha256"]
        composed.extra_disabled = True
        composed.extra_operation_ids = []
        composed.extra_operation_readback_sha = sample["extra_operation_readback_sha256"]
        composed.policy_checks = sample["extra_policy_readbacks"]
        composed.operation_checks = sample["extra_operation_readbacks"]
        composed.additional_owner_checks = sample["additional_owner_checks"]
        composed.primary_containment_checks = sample["primary_containment_checks"]
        composed.release_receipt = sample["release_receipt"]
        composed.additional_release_receipt = sample["additional_owner_checks"][-1]
        composed.observation = sample["acceptance_observations"][0]
        composed._publish_after_release()
        client.return_value.request.assert_not_called()
    artifact, _ = journey._read_artifact(path)
    assert artifact["result"] == expected_status
    row = journey.validate_source_setup_artifact(artifact, state, public, TARGET, RUNTIME)
    assert row["status"] == expected_status and row["evidence_kind"] == "PUBLIC_READ_ONLY"


def test_controlled_source_setup_does_not_replace_prior_ui_partial(tmp_path):
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
    state, public = baseline(), public_artifact()
    artifact = source_setup_artifact(state, public)
    source_setup_controlled(artifact)
    path = tmp_path / "public-source-setup.json"
    emit(path, artifact)
    checkpoint.binding.update(runtime=RUNTIME, source_setup={"artifact": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    original = {"number": 9, "status": "PARTIAL", "verification": {"evidence_artifact": "ui.json"}}
    checkpoint.binding["steps"][8] = copy.deepcopy(original)
    checkpoint.record_source_setup(state, public)
    assert checkpoint.binding["steps"][8] == original
