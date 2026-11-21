"""Reproducible connected campaign CLI entry point."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
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
        self.result = None
        self.returned_report = None

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
        self.returned_report = self.result or {
            "result": "PARTIAL", "run_id": kwargs["run_id"], "scoreboard": {},
        }
        return self.returned_report


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
    assert parsed.public_source_provider == "wwr"

    explicit = campaign.build_parser().parse_args([
        "--evidence-dir", "outputs/run", "--startup-receipt", "outputs/startup.json",
        "--build-receipt", "outputs/build.json", "--monitoring", "--source-setup",
        "--resume", "--stop-after-ui",
    ])
    assert explicit.monitoring and explicit.source_setup
    assert explicit.resume and explicit.stop_after_ui


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_campaign_accepts_only_explicit_supported_public_providers(provider):
    parsed = campaign.build_parser().parse_args([
        "--evidence-dir", "outputs/run", "--startup-receipt", "outputs/startup.json",
        "--build-receipt", "outputs/build.json", "--public-source-provider", provider,
    ])
    assert parsed.public_source_provider == provider


@pytest.mark.parametrize("provider", ["remoteok", "greenhouse", "https://example.test", ""])
def test_campaign_rejects_unsupported_public_provider_before_execution(provider):
    with pytest.raises(SystemExit) as failure:
        campaign.build_parser().parse_args([
            "--evidence-dir", "outputs/run", "--startup-receipt", "outputs/startup.json",
            "--build-receipt", "outputs/build.json", "--public-source-provider", provider,
        ])
    assert failure.value.code == 2


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
    assert "run_id" not in output
    assert output["launch"]["public_source_provider"] == "wwr"
    assert journey.startup_validations
    assert journey.run_calls == []
    assert source_setup_checks == ["validated"]


def test_validate_only_stdout_omits_arguments_paths_and_unknown_receipt_data(
        tmp_path, monkeypatch, capsys):
    startup, build, startup_path, build_path = _receipt_files(tmp_path)
    startup_path = startup_path.rename(startup_path.with_name("startup-PRIVATE-PATH-SENTINEL.json"))
    build_path = build_path.rename(build_path.with_name("build-PRIVATE-PATH-SENTINEL.json"))
    startup["unknown_private_field"] = {"nested": "PRIVATE-RECEIPT-SENTINEL"}
    startup_path.write_text(json.dumps(startup), encoding="utf-8")
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))
    monkeypatch.setattr(sys, "argv", ["PRIVATE-ARGV-SENTINEL"])
    args = _args(tmp_path / "outputs" / "PRIVATE-EVIDENCE-PATH-SENTINEL",
                 startup_path, build_path, "--validate-only", "--employer-verification")

    assert campaign.run_campaign(args, journey=journey) == 0

    rendered = capsys.readouterr().out
    for sentinel in ("PRIVATE-ARGV-SENTINEL", "PRIVATE-PATH-SENTINEL",
                     "PRIVATE-EVIDENCE-PATH-SENTINEL", "PRIVATE-RECEIPT-SENTINEL"):
        assert sentinel not in rendered
    output = json.loads(rendered)
    assert output["receipt_schema"] == "PASS"
    assert output["live_runtime"] == output["ui"] == output["public_operations"] == "NOT_RUN"
    assert "run_id" not in output and "target" not in output and "evidence" not in output
    assert "employer_verification" not in output["launch"]
    assert output["launch"]["source_commit"] == SOURCE["commit"]
    assert output["launch"]["startup_receipt_sha256"]
    assert journey.run_calls == []


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
        "public_source_provider": plan["public_source_provider"], "monitoring": False,
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


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_containment_is_lazy_and_binds_the_exact_confirmed_mock_application(tmp_path, monkeypatch, provider):
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch,
        flags=("--public-source-provider", provider))
    _bind_public_artifacts(journey, plan)
    calls = _patch_public_boundaries(monkeypatch, {
        "id": "application-1", "profile_id": "profile-1", "target": "MOCK",
        "state": "CONFIRMED", "job_id": "job-1",
        "confirmation": {"reference_number": "fixture-confirmed"}, "route_id": "route-1",
    })
    callback = campaign._lazy_containment(plan, args, journey, None)
    assert calls["http"] == calls["docker"] == 0
    assert calls["constructed"] == []

    wrong = {"provider": "remotive" if provider == "wwr" else "wwr", "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
    with pytest.raises(ValueError, match="identity"):
        callback("preflight", wrong)
    assert calls["http"] == 0

    context = {"provider": provider, "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
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


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_source_setup_wrapper_receives_validated_runtime_and_same_mock_baseline(
        tmp_path, monkeypatch, provider):
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch,
        flags=("--source-setup", "--public-source-provider", provider))
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
    context = {"provider": provider, "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
    callback("preflight", context)
    assert len(source_setup_calls) == 1
    _, kwargs = source_setup_calls[0]
    assert kwargs["primary_provider"] == provider
    assert kwargs["baseline"]["selected_application_route_id"] == "route-1"
    assert kwargs["runtime_identity"] == {
        "source_manifest_sha256": "a" * 64, "images": {"api": IMAGE_ID},
    }


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_normal_launch_forwards_resume_stop_and_explicit_monitoring(tmp_path, monkeypatch, capsys, provider):
    # run_campaign intentionally prepares process environment for the connected
    # journey. This unit test calls it in-process, so isolate and restore those
    # CLI-only settings before later Compose tests inspect the checkout.
    for name in (
            "COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME", "MJOBS_PORT",
            "MJOBS_OPERATION_MODE", "MJOBS_COMPOSE_MOCK", "MJOBS_IMAGE"):
        monkeypatch.setenv(name, "")
    startup, build, startup_path, build_path = _receipt_files(tmp_path)
    journey = FakeJourney(startup, build)
    monkeypatch.setattr(campaign, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Dockerfile"))
    callback = object()
    monkeypatch.setattr(campaign, "_lazy_containment", lambda *args: callback)
    args = _args(tmp_path / "outputs" / "campaign", startup_path, build_path,
                 "--resume", "--stop-after-ui", "--monitoring", "--public-source-provider", provider)

    assert campaign.run_campaign(args, journey=journey) == 2
    called = journey.run_calls[0]
    assert called["resume"] is True
    assert called["stop_after_ui"] is True
    assert called["monitoring"] is True
    assert called["public_source_provider"] == provider
    assert called["public_source_preflight"] is callback
    assert called["startup_receipt"] == startup_path.resolve()
    assert called["build_receipt"] == build_path.resolve()
    assert len(capsys.readouterr().out.splitlines()) == 2



@pytest.mark.parametrize("provider", ["remoteok", "", None])
def test_validate_plan_rejects_bypassed_parser_provider_before_receipt_or_runtime(tmp_path, provider):
    args = _args(tmp_path, tmp_path / "missing-startup", tmp_path / "missing-build")
    args.public_source_provider = provider
    with pytest.raises(ValueError, match="Unsupported public source provider"):
        campaign.validate_plan(args, journey=FakeJourney({}, {}))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_employer_opt_in_is_explicit_and_validate_only_has_no_runtime_actions(
        tmp_path, monkeypatch, capsys, enabled, provider):
    flags = ["--validate-only", "--public-source-provider", provider]
    if enabled:
        flags.append("--employer-verification")
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch, flags=tuple(flags))
    assert args.employer_verification is enabled
    assert plan["employer_verification"] is enabled
    assert plan["public_source_provider"] == provider
    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("Validate-only must not construct runtime containment")

    monkeypatch.setattr(campaign, "_lazy_containment", forbidden)
    assert campaign.run_campaign(args, journey=journey) == 0
    rendered = capsys.readouterr().out
    assert "employer_verification" not in rendered
    result = json.loads(rendered)
    assert result["launch"]["public_source_provider"] == provider
    assert result["live_runtime"] == result["ui"] == result["public_operations"] == "NOT_RUN"
    assert touched == journey.run_calls == []


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_non_boolean_employer_opt_in_is_rejected_before_missing_receipts(tmp_path, value):
    args = _args(tmp_path, tmp_path / "missing-startup", tmp_path / "missing-build")
    args.employer_verification = value
    with pytest.raises(ValueError, match="boolean"):
        campaign.validate_plan(args, journey=FakeJourney({}, {}))


@pytest.mark.parametrize("enabled", [False, True])
def test_campaign_forwards_the_exact_employer_opt_in_boolean_once(
        tmp_path, monkeypatch, capsys, enabled):
    for name in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME", "MJOBS_PORT",
                 "MJOBS_OPERATION_MODE", "MJOBS_COMPOSE_MOCK", "MJOBS_IMAGE"):
        monkeypatch.setenv(name, "")
    flags = ("--employer-verification",) if enabled else ()
    args, journey, _ = _prepared_plan(tmp_path, monkeypatch, flags=flags)
    callback = object()
    construction = []

    def lazy(*args):
        construction.append(True)
        return callback

    monkeypatch.setattr(campaign, "_lazy_containment", lazy)
    assert campaign.run_campaign(args, journey=journey) == 2
    assert construction == [True]
    assert len(journey.run_calls) == 1
    assert journey.run_calls[0]["employer_verification"] is enabled
    assert journey.run_calls[0]["public_source_preflight"] is callback
    launch, result = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "employer_verification" not in launch["launch"]
    assert result["result"] == "PARTIAL"


def test_campaign_stdout_projects_fixed_fields_and_preserves_private_report_and_opt_in(
        tmp_path, monkeypatch, capsys):
    for name in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME", "MJOBS_PORT",
                 "MJOBS_OPERATION_MODE", "MJOBS_COMPOSE_MOCK", "MJOBS_IMAGE"):
        monkeypatch.setenv(name, "")
    args, journey, _ = _prepared_plan(
        tmp_path, monkeypatch, flags=("--employer-verification",))
    monkeypatch.setattr(sys, "argv", ["PRIVATE-ARGV-SENTINEL"])
    journey.result = {
        "result": "PARTIAL", "run_id": "PRIVATE-RUN-ID-SENTINEL",
        "scoreboard": {"observed_PASS": 4, "PRIVATE-UNKNOWN-KEY": 1,
                       "unknown_nested": {"witness": "PRIVATE-WITNESS-SENTINEL",
                                          "confirmation": "PRIVATE-CONFIRMATION-SENTINEL"}},
        "unknown_nested": {"profile": "PRIVATE-REPORT-SENTINEL"},
    }
    callback = object()
    monkeypatch.setattr(campaign, "_lazy_containment", lambda *a: callback)

    assert campaign.run_campaign(args, journey=journey) == 2

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    launch, completion = [json.loads(line) for line in lines]
    for sentinel in ("PRIVATE-ARGV-SENTINEL", "PRIVATE-RUN-ID-SENTINEL",
                     "PRIVATE-WITNESS-SENTINEL", "PRIVATE-CONFIRMATION-SENTINEL",
                     "PRIVATE-REPORT-SENTINEL"):
        assert sentinel not in "\n".join(lines)
    assert launch["launch"]["source_commit"] == SOURCE["commit"]
    assert "command" not in launch["launch"]
    assert "employer_verification" not in launch["launch"]
    assert completion["result"] == "PARTIAL"
    assert completion["scoreboard"]["observed_PASS"] == 4
    assert "PRIVATE-UNKNOWN-KEY" not in completion["scoreboard"]
    assert journey.run_calls[0]["employer_verification"] is True
    assert journey.returned_report is journey.result


def _bind_employer_opt_in_artifacts(journey, plan):
    from tests.unit.test_acceptance_employer_verification_opt_in import (
        _controlled_profile, _controlled_ui_artifact,
    )

    runtime, application = _bind_public_artifacts(journey, plan)
    profile = _controlled_profile()
    journey.baseline.update(_controlled_ui_artifact(
        profile=profile, source=plan["source"], run_id=plan["run_id"], origin=plan["origin"]))
    journey.report["employer_verification"] = True
    return runtime, application, profile


def _patch_employer_opt_in_boundaries(monkeypatch, application, profile, *, status=200):
    import httpx

    real_client = httpx.Client
    calls = _patch_public_boundaries(monkeypatch, application)
    calls["paths"] = []

    def handle(request):
        assert request.method == "GET"
        calls["paths"].append(request.url.path)
        if request.url.path == "/profiles/profile-1/applications/application-1":
            return httpx.Response(200, json=application)
        assert request.url.path == "/profiles/profile-1"
        headers = {"content-type": "application/json"}
        if status == 302:
            headers["location"] = "https://example.invalid/must-not-follow"
        return httpx.Response(status, content=json.dumps(profile).encode(), headers=headers)

    def client(**kwargs):
        calls["http"] += 1
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return calls


@pytest.mark.parametrize("requested,saved", [
    (False, True), (False, 0), (False, "false"),
    (True, False), (True, 1), (True, "true"), (True, None), (True, "missing"),
])
def test_saved_employer_opt_in_mismatch_stops_before_any_admission_read(
        tmp_path, monkeypatch, requested, saved):
    flags = ("--employer-verification",) if requested else ()
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch, flags=flags)
    _, application, profile = _bind_employer_opt_in_artifacts(journey, plan)
    if saved == "missing":
        journey.report.pop("employer_verification")
    else:
        journey.report["employer_verification"] = saved
    calls = _patch_employer_opt_in_boundaries(monkeypatch, application, profile)
    callback = campaign._lazy_containment(plan, args, journey, None)
    with pytest.raises(ValueError, match="same-run UI baseline"):
        callback("preflight", {"provider": "wwr", "acceptance_run_id": plan["run_id"],
                               "base_url": plan["origin"]})
    assert calls["http"] == calls["docker"] == 0
    assert calls["paths"] == calls["constructed"] == []


def test_default_admission_keeps_legacy_missing_flag_and_adds_no_profile_read(tmp_path, monkeypatch):
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch)
    _, application = _bind_public_artifacts(journey, plan)
    assert "employer_verification" not in journey.report
    calls = _patch_employer_opt_in_boundaries(monkeypatch, application, None)
    callback = campaign._lazy_containment(plan, args, journey, None)
    callback("preflight", {"provider": "wwr", "acceptance_run_id": plan["run_id"],
                           "base_url": plan["origin"]})
    assert calls["paths"] == ["/profiles/profile-1/applications/application-1"]
    assert calls["http"] == calls["docker"] == len(calls["constructed"]) == 1


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_opt_in_admission_reads_exact_current_profile_before_owner_construction(
        tmp_path, monkeypatch, provider):
    args, journey, plan = _prepared_plan(tmp_path, monkeypatch,
        flags=("--employer-verification", "--public-source-provider", provider))
    _, application, profile = _bind_employer_opt_in_artifacts(journey, plan)
    calls = _patch_employer_opt_in_boundaries(monkeypatch, application, profile)
    callback = campaign._lazy_containment(plan, args, journey, None)
    assert calls["paths"] == calls["constructed"] == []
    context = {"provider": provider, "acceptance_run_id": plan["run_id"], "base_url": plan["origin"]}
    assert callback("preflight", context) == {"released": True}
    assert calls["paths"] == ["/profiles/profile-1/applications/application-1", "/profiles/profile-1"]
    assert calls["http"] == calls["docker"] == len(calls["constructed"]) == 1
    assert calls["owned_calls"] == [("preflight", context)]
    assert calls["constructed"][0]["baseline"]["employer_verification_opt_in"] == (
        journey.baseline["employer_verification_opt_in"])


@pytest.mark.parametrize("change", [
    "null", "list", "foreign", "version", "activation", "disabled", "parent-disabled",
    "malformed-version", "policy-override", "unavailable", "redirect",
])
def test_opt_in_admission_rejects_current_profile_drift_before_docker_or_pause(
        tmp_path, monkeypatch, change):
    import copy
    import httpx

    args, journey, plan = _prepared_plan(tmp_path, monkeypatch, flags=("--employer-verification",))
    _, application, baseline_profile = _bind_employer_opt_in_artifacts(journey, plan)
    profile = copy.deepcopy(baseline_profile)
    preferences = profile["preferences"]["company_discovery"]
    status = 200
    if change == "null": profile = None
    elif change == "list": profile = []
    elif change == "foreign": profile["id"] = "another-profile"
    elif change == "version": profile["version"] = "4" * 64
    elif change == "activation": preferences["employer_verification"]["activation_id"] = "b" * 32
    elif change == "disabled": preferences["employer_verification"] = {"version": 1, "mode": "DISABLED", "activation_id": None}
    elif change == "parent-disabled": preferences["company_discovery_enabled"] = False
    elif change == "malformed-version": preferences["employer_verification"]["version"] = True
    elif change == "policy-override": preferences["employer_verification"]["rate_limit"] = 1000
    elif change == "unavailable": status = 503
    elif change == "redirect": status = 302
    calls = _patch_employer_opt_in_boundaries(monkeypatch, application, profile, status=status)
    callback = campaign._lazy_containment(plan, args, journey, None)
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        callback("preflight", {"provider": "wwr", "acceptance_run_id": plan["run_id"],
                               "base_url": plan["origin"]})
    assert calls["paths"] == ["/profiles/profile-1/applications/application-1", "/profiles/profile-1"]
    assert calls["http"] == 1 and calls["docker"] == 0
    assert calls["constructed"] == []
