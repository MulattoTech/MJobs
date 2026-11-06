"""Reproducible connected campaign CLI entry point."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest


from cli import acceptance_campaign as campaign


SOURCE = {
    "commit": "a" * 40,
    "tree": "b" * 40,
    "working_tree_diff_sha256": "c" * 64,
    "working_tree_status_sha256": "d" * 64,
}
TARGET = {
    "project_name": "mjobs-fixture-012345abcdef",
    "port": 18777,
    "base_url": "http://127.0.0.1:18777",
}
IMAGE_ID = "sha256:" + "e" * 64


class FakeJourney:
    ROOT = Path(".")
    SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

    def __init__(self, startup, build):
        self.artifacts = {"startup.json": startup, "build.json": build}
        self.run_calls = []
        self.startup_validations = []
        self.report = None
        self.baseline = None

    def _read_artifact(self, path):
        if Path(path).name == "journey.json" and self.report is not None:
            return self.report, "1" * 64
        if Path(path).name == "ui.json" and self.baseline is not None:
            return self.baseline, "2" * 64
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream)
        return value, "f" * 64

    def current_source(self):
        return SOURCE

    @staticmethod
    def validate_target(origin, project, port):
        return {"base_url": origin, "project_name": project, "port": port}

    def validate_startup_receipt(self, receipt, source, run_id, target, runtime):
        self.startup_validations.append((receipt, source, run_id, target, runtime))

    @staticmethod
    def validate_frontend_build_receipt(receipt, source, runtime):
        return {"status": "PASS"}

    @staticmethod
    def _instant(value):
        return datetime.fromisoformat(value).astimezone(timezone.utc)

    @staticmethod
    def _identity(baseline):
        assert baseline["profile_id"] == "profile-1"

    def run(self, **kwargs):
        self.run_calls.append(kwargs)
        return {"result": "PARTIAL", "run_id": kwargs["run_id"], "scoreboard": {}}


def _receipt_files(tmp_path, *, startup_source=None, app=None):
    root = tmp_path
    outputs = root / "outputs"
    outputs.mkdir()
    (root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    startup = {
        "source": startup_source or SOURCE,
        "acceptance_run_id": "run-1",
        "target": TARGET,
        "command": ["docker", "compose", "-f", "compose.yml", "up", "-d"],
        "containers": {"api": "container-api", "scheduler": "container-scheduler"},
        "images": {"api": IMAGE_ID},
        "container_created_at": {},
        "service_started_at": {},
        "health": {},
        "migration": {},
        "started_at_utc": "2026-11-06T12:00:00+00:00",
    }
    build = {
        "source_commit": SOURCE["commit"],
        "source_tree": SOURCE["tree"],
        "image_id": IMAGE_ID,
        "frontend_assets": {"index.html": "f" * 64},
        "completed_at_utc": "2026-11-06T11:00:00+00:00",
    }
    startup_path, build_path = outputs / "startup.json", outputs / "build.json"
    startup_path.write_text(json.dumps(startup), encoding="utf-8")
    build_path.write_text(json.dumps(build), encoding="utf-8")
    return startup, build, startup_path, build_path


def _args(evidence, startup, build, *flags):
    return campaign.build_parser().parse_args([
        "--evidence-dir", str(evidence), "--startup-receipt", str(startup),
        "--build-receipt", str(build), *flags,
    ])


def _prepared_plan(tmp_path, monkeypatch, *, flags=()):
    startup, build, startup_path, build_path = _receipt_files(tmp_path)
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))
    args = _args(tmp_path / "outputs" / "campaign", startup_path, build_path, *flags)
    plan = campaign.validate_plan(args, journey=journey)
    return args, journey, plan


def test_campaign_cli_is_importable_from_the_selected_checkout():
    assert importlib.util.find_spec("cli.acceptance_campaign") is not None


def test_monitoring_and_source_setup_are_explicit_options():
    parsed = campaign.build_parser().parse_args([
        "--evidence-dir", "outputs/run", "--startup-receipt", "outputs/startup.json",
        "--build-receipt", "outputs/build.json",
    ])
    assert parsed.monitoring is False
    assert parsed.source_setup is False
    assert parsed.resume is False
    assert parsed.stop_after_ui is False

    explicit = campaign.build_parser().parse_args([
        "--evidence-dir", "outputs/run", "--startup-receipt", "outputs/startup.json",
        "--build-receipt", "outputs/build.json", "--monitoring", "--source-setup",
        "--resume", "--stop-after-ui",
    ])
    assert explicit.monitoring and explicit.source_setup
    assert explicit.resume and explicit.stop_after_ui


def test_validate_only_checks_receipts_without_runtime_or_journey_calls(tmp_path, monkeypatch, capsys):
    startup, build, startup_path, build_path = _receipt_files(tmp_path)
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))
    source_setup_checks = []
    monkeypatch.setattr(campaign, "_source_setup_support",
                        lambda: source_setup_checks.append("validated") or object())

    args = _args(tmp_path / "outputs" / "campaign", startup_path, build_path,
                 "--validate-only", "--source-setup", "--monitoring")
    assert campaign.run_campaign(args, journey=journey) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["receipt_schema"] == "PASS"
    assert output["live_runtime"] == output["ui"] == output["public_operations"] == "NOT_RUN"
    assert output["run_id"] == "run-1"
    assert journey.startup_validations
    assert journey.run_calls == []
    assert source_setup_checks == ["validated"]


def test_validate_only_rejects_receipt_source_mismatch_before_journey_execution(tmp_path, monkeypatch):
    startup, build, startup_path, build_path = _receipt_files(
        tmp_path, startup_source={**SOURCE, "commit": "9" * 40})
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))

    args = _args(tmp_path / "outputs" / "campaign", startup_path, build_path, "--validate-only")
    with pytest.raises(ValueError, match="exact clean checkout"):
        campaign.run_campaign(args, journey=journey)
    assert journey.run_calls == []


def test_validate_only_rejects_startup_build_image_mismatch(tmp_path, monkeypatch):
    startup, build, startup_path, build_path = _receipt_files(tmp_path)
    build["image_id"] = "sha256:" + "9" * 64
    build_path.write_text(json.dumps(build), encoding="utf-8")
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))

    args = _args(tmp_path / "outputs" / "campaign", startup_path, build_path, "--validate-only")
    with pytest.raises(ValueError, match="different API images"):
        campaign.validate_plan(args, journey=journey)
    assert journey.run_calls == []


@pytest.mark.parametrize("outside", ["..\\outside", "C:\\private\\evidence"])
def test_evidence_path_must_remain_inside_ignored_checkout_outputs(tmp_path, monkeypatch, outside):
    startup, build, startup_path, build_path = _receipt_files(tmp_path)
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))

    args = _args(outside, startup_path, build_path, "--validate-only")
    with pytest.raises(ValueError, match="ignored outputs"):
        campaign.validate_plan(args, journey=journey)


def _bind_public_artifacts(journey, plan, *, application=None):
    journey.baseline = {
        "source": plan["source"], "acceptance_run_id": plan["run_id"],
        "target_origin": plan["origin"], "profile_id": "profile-1",
        "application_id": "application-1", "selected_application_job_id": "job-1",
        "confirmation": {"reference_number": "fixture-confirmed"},
    }
    runtime = {
        "compose_files": [str(path) for path in plan["compose_files"]],
        "source_manifest_sha256": "a" * 64,
        "images": {"api": plan["image_id"]},
        "containers": {"api": "container-api"},
    }
    journey.report = {
        "source": plan["source"], "run_id": plan["run_id"], "target": plan["target"],
        "public_source_provider": "wwr", "monitoring": False,
        "segments": {"ui": {"status": "COMPLETE", "artifact": "ui.json", "sha256": "2" * 64}},
        "runtime_checks": {"ui": {"after": runtime}},
    }
    journey.current_source = lambda: plan["source"]
    journey.validate_frontend_build_receipt = lambda *args: {"status": "PASS"}
    if application is None:
        application = {
            "id": "application-1", "profile_id": "profile-1", "target": "MOCK",
            "state": "CONFIRMED", "job_id": "job-1",
            "confirmation": {"reference_number": "fixture-confirmed"},
            "route_id": "route-1",
        }
    return runtime, application


def _patch_public_boundaries(monkeypatch, application):
    import httpx
    from cli import acceptance_containment, acceptance_source_containment

    calls = {"http": 0, "docker": 0, "constructed": []}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return application

    class Client:
        def __init__(self, **kwargs):
            calls["http"] += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, path):
            calls["path"] = path
            return Response()

    class Docker:
        def inspect(self, container_id):
            calls["docker"] += 1
            calls["container_id"] = container_id
            return {"image_id": IMAGE_ID, "working_dir": "/app", "image_ref": IMAGE_ID}

    class Owned:
        def __init__(self, **kwargs):
            calls["constructed"].append(kwargs)

        def __call__(self, stage, context):
            calls.setdefault("owned_calls", []).append((stage, context))
            return {"released": True}

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(acceptance_containment, "DockerCLI", Docker)
    monkeypatch.setattr(acceptance_source_containment, "OwnedPublicSourceContainment", Owned)
    return calls


def test_containment_is_lazy_and_binds_the_exact_confirmed_mock_application(tmp_path, monkeypatch):
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch)
    _bind_public_artifacts(journey, plan)
    calls = _patch_public_boundaries(monkeypatch, {
        "id": "application-1", "profile_id": "profile-1", "target": "MOCK",
        "state": "CONFIRMED", "job_id": "job-1",
        "confirmation": {"reference_number": "fixture-confirmed"}, "route_id": "route-1",
    })
    callback = campaign._lazy_containment(plan, args, journey, None)
    assert calls["http"] == calls["docker"] == 0
    assert calls["constructed"] == []

    wrong = {"provider": "remotive", "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
    with pytest.raises(ValueError, match="identity"):
        callback("preflight", wrong)
    assert calls["http"] == 0

    context = {"provider": "wwr", "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
    assert callback("preflight", context) == {"released": True}
    assert calls["path"] == "/profiles/profile-1/applications/application-1"
    assert calls["docker"] == 1
    assert calls["constructed"][0]["baseline"]["selected_application_route_id"] == "route-1"


@pytest.mark.parametrize("change", [
    {"target": "WORKDAY"}, {"state": "SUBMITTING"}, {"profile_id": "profile-other"},
    {"job_id": "job-other"}, {"route_id": "unsafe route"},
])
def test_public_admission_rejects_nonconfirmed_or_mismatched_application_before_docker(
        tmp_path, monkeypatch, change):
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch)
    _bind_public_artifacts(journey, plan)
    app = {
        "id": "application-1", "profile_id": "profile-1", "target": "MOCK",
        "state": "CONFIRMED", "job_id": "job-1",
        "confirmation": {"reference_number": "fixture-confirmed"}, "route_id": "route-1",
        **change,
    }
    calls = _patch_public_boundaries(monkeypatch, app)
    callback = campaign._lazy_containment(plan, args, journey, None)
    context = {"provider": "wwr", "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
    with pytest.raises(ValueError, match="Selected MOCK application"):
        callback("preflight", context)
    assert calls["http"] == 1
    assert calls["docker"] == 0
    assert calls["constructed"] == []


def test_source_setup_wrapper_receives_validated_runtime_and_same_mock_baseline(
        tmp_path, monkeypatch):
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch, flags=("--source-setup",))
    _bind_public_artifacts(journey, plan)
    calls = _patch_public_boundaries(monkeypatch, {
        "id": "application-1", "profile_id": "profile-1", "target": "MOCK",
        "state": "CONFIRMED", "job_id": "job-1",
        "confirmation": {"reference_number": "fixture-confirmed"}, "route_id": "route-1",
    })
    source_setup_calls = []

    class SourceSetup:
        @staticmethod
        def compose_owned_public_containment(owned, **kwargs):
            source_setup_calls.append((owned, kwargs))
            return owned

    callback = campaign._lazy_containment(plan, args, journey, SourceSetup)
    context = {"provider": "wwr", "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
    callback("preflight", context)
    assert len(source_setup_calls) == 1
    _, kwargs = source_setup_calls[0]
    assert kwargs["baseline"]["selected_application_route_id"] == "route-1"
    assert kwargs["runtime_identity"] == {
        "source_manifest_sha256": "a" * 64, "images": {"api": IMAGE_ID},
    }


def test_normal_launch_forwards_resume_stop_and_explicit_monitoring(tmp_path, monkeypatch, capsys):
    startup, build, startup_path, build_path = _receipt_files(tmp_path)
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))
    callback = object()
    monkeypatch.setattr(campaign, "_lazy_containment", lambda *args: callback)
    args = _args(tmp_path / "outputs" / "campaign", startup_path, build_path,
                 "--resume", "--stop-after-ui", "--monitoring")

    assert campaign.run_campaign(args, journey=journey) == 2
    called = journey.run_calls[0]
    assert called["resume"] is True
    assert called["stop_after_ui"] is True
    assert called["monitoring"] is True
    assert called["public_source_provider"] == "wwr"
    assert called["public_source_preflight"] is callback
    assert called["startup_receipt"] == startup_path.resolve()
    assert called["build_receipt"] == build_path.resolve()
    assert len(capsys.readouterr().out.splitlines()) == 2
