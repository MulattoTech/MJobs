"""Launch a connected MJ-REQ-292 campaign from this exact frozen checkout.

The command consumes operator-produced startup and frontend-build receipts; it
does not provision a stack or edit acceptance ledgers. Running the command opts
into a supported public-read-only campaign (WWR by default). Step 9 setup and monitoring
remain separate explicit flags. Real employer application submission stays
disabled by the journey's fixture contract.

Invoke from the selected checkout with ``python -m cli.acceptance_campaign``.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
CAMPAIGN_RESULTS = frozenset({"PASS", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED"})
SCOREBOARD_FIELDS = ("observed_PASS", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED")


def _campaign_status(value):
    return value if isinstance(value, str) and value in CAMPAIGN_RESULTS else "UNKNOWN"


def _launch_console_summary(plan, args):
    """Project launch metadata to fixed, non-path console fields."""
    return {
        "source_setup": args.source_setup is True,
        "monitoring": args.monitoring is True,
        "public_source_provider": plan["public_source_provider"],
        "source_commit": plan["source"]["commit"],
        "source_tree": plan["source"]["tree"],
        "image_id": plan["image_id"],
        "startup_receipt_sha256": plan["startup_sha256"],
        "build_receipt_sha256": plan["build_sha256"],
    }


def _scoreboard_console_summary(report):
    source = report.get("scoreboard")
    source = source if isinstance(source, dict) else {}
    return {
        field: value if type(value) is int and value >= 0 else 0
        for field in SCOREBOARD_FIELDS
        for value in (source.get(field, 0),)
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True,
                        help="New or resumed evidence directory inside this checkout's ignored outputs")
    parser.add_argument("--startup-receipt", required=True,
                        help="Operator-recorded startup receipt for this exact source, image and fixture target")
    parser.add_argument("--build-receipt", required=True,
                        help="Operator-recorded frontend build receipt for the same source and immutable image")
    parser.add_argument("--source-label", default="https://github.com/MulattoTech/MJobs")
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--public-source-provider", choices=("wwr", "remotive"), default="wwr",
                        help="Use one supported public source for this run; never retry or switch providers automatically")
    parser.add_argument("--resume", action="store_true",
                        help="Resume only a valid checkpoint in the same evidence directory")
    parser.add_argument("--stop-after-ui", action="store_true",
                        help="Stop after the connected UI segment; do not enter public collection")
    parser.add_argument("--source-setup", action="store_true",
                        help="Observe step 9 with the other supported source, then disable it before primary collection")
    parser.add_argument("--monitoring", action="store_true",
                        help="Explicitly include same-profile natural fixture monitoring and restart observations")
    parser.add_argument("--employer-verification", action="store_true",
                        help="Explicitly save the profile's employer-verification checkbox during the UI segment")
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate receipts and checkout bindings without Docker, API or journey execution")
    return parser


def _path_from_root(root, value, *, strict):
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=strict)


def _inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_plan(args, *, journey=None, root=None):
    """Validate exact local inputs only; this function makes no runtime calls."""
    employer_verification = getattr(args, "employer_verification", False)
    if type(employer_verification) is not bool:
        raise ValueError("Employer-verification opt-in must be boolean")
    if args.public_source_provider not in {"wwr", "remotive"}:
        raise ValueError("Unsupported public source provider")
    if journey is None:
        from cli import acceptance_journey as journey
    root = (Path(root) if root is not None else ROOT).resolve(strict=True)
    if Path.cwd().resolve(strict=True) != root:
        raise ValueError("Run the campaign from the selected frozen checkout root")

    output_root = (root / "outputs").resolve(strict=True)
    evidence_dir = _path_from_root(root, args.evidence_dir, strict=False)
    startup_path = _path_from_root(root, args.startup_receipt, strict=True)
    build_path = _path_from_root(root, args.build_receipt, strict=True)
    if not _inside(evidence_dir, output_root):
        raise ValueError("Evidence directory must be inside this checkout's ignored outputs")
    if not startup_path.is_file() or not _inside(startup_path, output_root):
        raise ValueError("Startup receipt must be a file inside this checkout's outputs")
    if not build_path.is_file() or not _inside(build_path, output_root):
        raise ValueError("Build receipt must be a file inside this checkout's outputs")

    startup, startup_sha = journey._read_artifact(startup_path)
    build, build_sha = journey._read_artifact(build_path)
    source = journey.current_source()
    if (startup.get("source") != source
            or build.get("source_commit") != source["commit"]
            or build.get("source_tree") != source["tree"]):
        raise ValueError("Startup/build receipts are not for this exact clean checkout")
    image_id = build.get("image_id")
    if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
        raise ValueError("Build receipt must identify an immutable Docker image")
    if startup.get("images", {}).get("api") != image_id:
        raise ValueError("Startup/build receipts identify different API images")

    run_id = startup.get("acceptance_run_id")
    target = startup.get("target")
    if not isinstance(run_id, str) or not run_id or not isinstance(target, dict):
        raise ValueError("Startup receipt lacks its run or target identity")
    project, port, origin = target.get("project_name"), target.get("port"), target.get("base_url")
    if journey.validate_target(origin, project, port) != target:
        raise ValueError("Startup target is not the exact owned loopback fixture")

    command = startup.get("command")
    if not isinstance(command, list) or any(not isinstance(value, str) for value in command):
        raise ValueError("Startup receipt lacks its executed Compose command")
    compose_files = []
    for index, value in enumerate(command[:-1]):
        if value in {"-f", "--file"}:
            compose_files.append(_path_from_root(root, command[index + 1], strict=True))
    if not compose_files or compose_files[0] != (root / "compose.yml").resolve(strict=True):
        raise ValueError("Startup command must preserve the ordered root-first Compose files")
    if any(not path.is_file() or not _inside(path, root) for path in compose_files):
        raise ValueError("Startup Compose files must exist inside the selected frozen checkout")

    receipt_runtime = {key: startup[key] for key in (
        "containers", "images", "container_created_at", "service_started_at", "health", "migration")}
    receipt_runtime["compose_files"] = [str(path) for path in compose_files]
    dockerfile = subprocess.run(
        ["git", "show", source["commit"] + ":infra/docker/Dockerfile"],
        cwd=root, check=True, timeout=30, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    receipt_runtime.update(
        dockerfile_sha256=hashlib.sha256(dockerfile).hexdigest(),
        frontend_assets=build.get("frontend_assets"),
    )
    journey.validate_startup_receipt(startup, source, run_id, target, receipt_runtime)
    journey.validate_frontend_build_receipt(build, source, receipt_runtime)
    if ("build_completed_at_utc" in build
            and build["completed_at_utc"] != build["build_completed_at_utc"]):
        raise ValueError("Normalized build completion differs from the recorded build completion")
    if journey._instant(build["completed_at_utc"]) > journey._instant(startup["started_at_utc"]):
        raise ValueError("The recorded build must complete before the initial Compose start")

    return {
        "root": root, "evidence_dir": evidence_dir,
        "startup_path": startup_path, "startup_sha256": startup_sha,
        "build_path": build_path, "build_sha256": build_sha,
        "startup": startup, "build": build, "source": source,
        "run_id": run_id, "target": target, "project": project,
        "port": port, "origin": origin, "image_id": image_id,
        "compose_files": compose_files, "public_source_provider": args.public_source_provider,
        "employer_verification": employer_verification,
    }


def _source_setup_support():
    from cli import acceptance_source_containment, acceptance_journey, acceptance_source_setup
    if (not callable(getattr(acceptance_source_containment.OwnedPublicSourceContainment,
                                    "cleanup_owned_child_sources", None))
            or not callable(getattr(acceptance_journey, "validate_source_setup_artifact", None))
            or not callable(getattr(acceptance_source_containment.OwnedPublicSourceContainment,
                                    "bind_additional_source", None))
            or not callable(getattr(acceptance_source_containment.OwnedPublicSourceContainment,
                                    "additional_source", None))
            or not callable(getattr(acceptance_source_setup,
                                    "compose_owned_public_containment", None))):
        raise ValueError("Source setup requires the integrated reviewed validator and owner")
    return acceptance_source_setup


def _lazy_containment(plan, args, journey, source_setup):
    import httpx
    from cli.acceptance_containment import DockerCLI
    from cli.acceptance_source_containment import OwnedPublicSourceContainment
    from cli.ui_e2e import validate_employer_verification_witness

    root, directory = plan["root"], plan["evidence_dir"]
    source, run_id, target = plan["source"], plan["run_id"], plan["target"]
    project, origin, compose_files = plan["project"], plan["origin"], plan["compose_files"]
    provider = plan["public_source_provider"]
    employer_verification = plan["employer_verification"]

    class LazyOwnedContainment:
        def __init__(self):
            self.instance = None

        def __call__(self, stage, context):
            if (context.get("provider") != provider
                    or context.get("acceptance_run_id") != run_id
                    or context.get("base_url") != origin):
                raise ValueError("Public callback identity differs from the selected provider plan")
            if self.instance is None:
                if stage != "preflight":
                    raise ValueError("Containment must begin at the first helper preflight")
                report, _ = journey._read_artifact(directory / "journey.json")
                baseline, ui_sha = journey._read_artifact(directory / "ui.json")
                ui_reference = report.get("segments", {}).get("ui", {})
                if (ui_reference.get("status") != "COMPLETE"
                        or ui_reference.get("artifact") != "ui.json"
                        or ui_reference.get("sha256") != ui_sha
                        or report.get("source") != source or report.get("run_id") != run_id
                        or report.get("target") != target
                        or report.get("public_source_provider") != provider
                        or report.get("monitoring") is not bool(args.monitoring)
                        or report.get("employer_verification", False) is not employer_verification
                        or baseline.get("source") != source
                        or baseline.get("acceptance_run_id") != run_id
                        or baseline.get("target_origin") != origin
                        or journey.current_source() != source):
                    raise ValueError("Public admission requires the verified same-run UI baseline")
                journey._identity(baseline)
                validate_employer_verification_witness(baseline, employer_verification)
                runtime = report["runtime_checks"]["ui"]["after"]
                journey.validate_frontend_build_receipt(plan["build"], source, runtime)
                if runtime["compose_files"] != [str(path) for path in compose_files]:
                    raise ValueError("UI runtime Compose identity changed")

                # The UI evidence does not carry route_id; bind it from the exact
                # selected confirmed MOCK application before creating containment.
                with httpx.Client(base_url=origin, timeout=15, trust_env=False,
                                  follow_redirects=False) as client:
                    response = client.get(
                        "/profiles/" + baseline["profile_id"]
                        + "/applications/" + baseline["application_id"])
                    response.raise_for_status()
                    app = response.json()
                    if employer_verification:
                        response = client.get("/profiles/" + baseline["profile_id"])
                        response.raise_for_status()
                        current_profile = response.json()
                        if not isinstance(current_profile, dict):
                            raise ValueError("Current employer-verification profile readback is invalid")
                        validate_employer_verification_witness(
                            baseline, True, current_profile=current_profile)
                if (app.get("id") != baseline["application_id"]
                        or app.get("profile_id") != baseline["profile_id"]
                        or app.get("target") != "MOCK" or app.get("state") != "CONFIRMED"
                        or app.get("job_id") != baseline["selected_application_job_id"]
                        or app.get("confirmation") != baseline.get("confirmation")
                        or not isinstance(app.get("route_id"), str)
                        or not journey.SAFE_ID.fullmatch(app["route_id"])
                        or baseline.get("selected_application_route_id", app["route_id"])
                        != app["route_id"]):
                    raise ValueError("Selected MOCK application changed before public admission")
                owned_baseline = {**baseline, "selected_application_route_id": app["route_id"]}
                metadata = DockerCLI().inspect(runtime["containers"]["api"])
                if (metadata.get("image_id") != plan["build"]["image_id"]
                        or metadata.get("working_dir") != "/app"):
                    raise ValueError("API immutable image or container working directory changed")
                owned = OwnedPublicSourceContainment(
                    project_name=project, base_url=origin, source_label=args.source_label,
                    image_ref=metadata["image_ref"], image_id=plan["build"]["image_id"],
                    source_revision=source["commit"], source_tree=source["tree"],
                    compose_working_dir=root, container_working_dir="/app",
                    compose_files=compose_files, run_dir=directory,
                    baseline=owned_baseline, repository_root=root,
                    owner="mj292-" + run_id, lease_seconds=args.lease_seconds,
                )
                if source_setup is not None:
                    self.instance = source_setup.compose_owned_public_containment(
                        owned, base_url=origin, baseline=owned_baseline,
                        acceptance_run_id=run_id, primary_provider=provider,
                        artifact_path=directory / "public-source-setup.json",
                        runtime_identity={key: runtime[key]
                                          for key in ("source_manifest_sha256", "images")},
                    )
                else:
                    self.instance = owned
            return self.instance(stage, context)

    return LazyOwnedContainment()


def run_campaign(args, *, journey=None):
    if journey is None:
        from cli import acceptance_journey as journey
    plan = validate_plan(args, journey=journey)
    source_setup = _source_setup_support() if args.source_setup else None
    if args.validate_only:
        print(json.dumps({
            "receipt_schema": "PASS", "live_runtime": "NOT_RUN", "ui": "NOT_RUN",
            "public_operations": "NOT_RUN", "source_commit": plan["source"]["commit"],
            "source_tree": plan["source"]["tree"], "image_id": plan["image_id"],
            "startup_receipt_sha256": plan["startup_sha256"],
            "build_receipt_sha256": plan["build_sha256"],
            "launch": _launch_console_summary(plan, args),
        }, sort_keys=True))
        return 0

    os.environ["COMPOSE_FILE"] = os.pathsep.join(str(path) for path in plan["compose_files"])
    os.environ["COMPOSE_PROFILES"] = "fixture"
    os.environ["COMPOSE_PROJECT_NAME"] = plan["project"]
    os.environ["MJOBS_PORT"] = str(plan["port"])
    os.environ["MJOBS_OPERATION_MODE"] = "FIXTURE"
    os.environ["MJOBS_COMPOSE_MOCK"] = "1"
    os.environ["MJOBS_IMAGE"] = plan["image_id"]
    print(json.dumps({"launch": _launch_console_summary(plan, args)}, sort_keys=True), flush=True)

    preflight = _lazy_containment(plan, args, journey, source_setup)
    report = journey.run(
        base_url=plan["origin"], evidence_dir=plan["evidence_dir"],
        project_name=plan["project"], port=plan["port"], run_id=plan["run_id"],
        resume=args.resume, stop_after_ui=args.stop_after_ui,
        startup_receipt=plan["startup_path"], build_receipt=plan["build_path"],
        public_source_provider=plan["public_source_provider"], public_source_preflight=preflight,
        monitoring=args.monitoring,
        employer_verification=plan["employer_verification"],
    )
    print(json.dumps({
        "result": _campaign_status(report.get("result")),
        "scoreboard": _scoreboard_console_summary(report),
    }, sort_keys=True))
    return 0 if report["result"] == "PASS" else 2


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_campaign(args)


if __name__ == "__main__":
    raise SystemExit(main())
