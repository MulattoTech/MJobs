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
           "images": {"api": "sha256:" + "e" * 64}, "containers": {"api": "container-1"},
           "frontend_assets": {"index.html": "f" * 64}, "dockerfile_sha256": "2" * 64}


@pytest.fixture(autouse=True)
def bound_archive(monkeypatch):
    monkeypatch.setattr(journey, "source_archive_sha256", lambda _: "1" * 64)


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
            "execution_scope": "LIVE_PUBLIC_READ_ONLY", "external_submission_enabled": False,
            "observed_real_job_ids": ["real-job-1"], "selected_application_job_id": "mock-job-1",
            "selected_application_route_id": "mock-route-1", "cleanup": {"status": "RESTORED_DISABLED"},
            "acceptance_observations": [row]}


def test_public_stage_requires_explicit_provider_and_containment_callback(tmp_path):
    with patch.object(journey.ui_e2e, "run") as ui:
        with pytest.raises(ValueError, match="containment"):
            invoke(tmp_path, public_source_provider="wwr")
    ui.assert_not_called()


def test_public_provider_stage_rebases_only_inventory_and_keeps_mock_application(tmp_path):
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
            patch.object(journey, "recapture_public_baseline", side_effect=recapture), \
            patch.object(journey.ui_e2e, "run", side_effect=lambda _, path, **kw: emit(path, baseline())), \
            patch.object(journey.recovery, "run", side_effect=recovery):
        report = invoke(tmp_path, public_source_provider="wwr",
                        public_source_preflight=lambda stage, context: events.append(stage))
    assert events == ["public", "release", "recovery"]
    assert report["steps"][10]["status"] == "PARTIAL"
    assert report["steps"][10]["credit_withheld"] == "PUBLIC_BROWSER_NOT_OBSERVED"
    assert report["steps"][10]["observation"]["evidence_kind"] == "PUBLIC_READ_ONLY"
    assert report["selected_application_job_id"] == "mock-job-1"
    assert report["observed_real_job_ids"] == ["real-job-1"]
    assert report["result"] == "PARTIAL"
    restored = journey.JourneyCheckpoint.resume(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET, "wwr").payload()
    assert restored["steps"][10]["verification"]["resume_imported"] is True
    assert restored["derived_baseline"] == report["derived_baseline"]
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
    artifact = application_stage_artifact()
    checkpoint = journey.JourneyCheckpoint.create(tmp_path / "journey.json", RUN_ID, SOURCE, TARGET)
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


@pytest.mark.parametrize("step", [44, 45, 46, 47, 48])
def test_review_stages_require_actual_stage_lineage(step):
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
