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


def test_initial_ledger_validates_and_reports_recomputed_x_of_60():
    report = ledger.validate_ledger(make_ledger(), ROOT)
    assert report.errors == []
    assert report.counts == {"PASS": 0, "PARTIAL": 0, "FAIL": 0,
                             "NOT_RUN": 60, "BLOCKED": 0}
    assert ledger.render_report(report) == (
        "MJ-REQ-292: 0/60 recorded PASS claims; structural validation only "
        "(runtime/review/integration not certified); 0 PARTIAL, 0 FAIL, 60 NOT_RUN, 0 BLOCKED"
    )


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


def test_pass_requires_current_artifact_hash_source_sha_and_continuity(tmp_path):
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

