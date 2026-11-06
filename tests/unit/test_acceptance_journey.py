"""Failure controls for the connected journey coordinator; no browser or Docker."""
import copy
import hashlib
import json
from pathlib import Path
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


def baseline():
    return {**IDENTITY, "source": SOURCE, "acceptance_run_id": RUN_ID,
            "timestamp": journey.utc_now(), "result": "PARTIAL", "mode": "ONBOARDING_FIXTURE",
            "target_origin": TARGET["base_url"],
            "external_submission_enabled": False,
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
        with pytest.raises(ValueError, match="registered"):
            journey.numbered_observations(artifact, segment="recovery" if step >= 54 else "ui",
                                          run_id=RUN_ID, source=SOURCE, identity=IDENTITY)


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
    for key, value in (("source_commit", "c" * 40), ("source_tree", "c" * 40),
                       ("image_id", "sha256:" + "c" * 64), ("frontend_assets", {"index.html": "c" * 64}),
                       ("source_archive_sha256", "c" * 64), ("dockerfile_sha256", "c" * 64)):
        with pytest.raises(ValueError, match="frontend"):
            journey.validate_frontend_build_receipt({**receipt, key: value}, SOURCE, RUNTIME)


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
