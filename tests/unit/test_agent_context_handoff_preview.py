"""Synthetic-only opt-in handoff bootstrap compatibility tests.

No real user handoff bytes or private historical archive are exported.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.agent_context import bootstrap, main
from tools.verification.handoff_archive import create_archive


def _git(repo: Path, *args: str):
    p = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=True,
    )
    return p.stdout.strip()


@pytest.fixture
def scenario(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "planning").mkdir(parents=True)
    original = (
        b"## LATEST synthetic checkpoint\r\n"
        b"THIS_IS_THE_REVIEWED_CURRENT_SECTION\r\n"
        b"## HISTORICAL synthetic checkpoint\n"
        b"THIS_IS_AN_OUTDATED_TRAILING_SECTION\n"
    )
    source = repo / "planning" / "SESSION_HANDOFF.md"
    source.write_bytes(original)
    (repo / "planning" / "REQUIREMENT_INDEX.json").write_text(
        json.dumps({"summary": {"PARTIAL": 1}, "requirements": []}),
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic Test")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic base")

    digest = hashlib.sha256(original).hexdigest()
    private = tmp_path / "private-outside-any-git-checkout"
    private.mkdir()
    create_archive(source, private, digest)
    snapshot = private / ("sha256-" + digest)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    review_file = tmp_path / "owned-review.json"
    review = {
        "schema_version": 1,
        "source_sha256": digest,
        "reviewed_by": "Synthetic Reviewer",
        "reviewed_at_utc": "2026-11-20T20:00:00Z",
        "review_reference": "synthetic evidence only",
        "entries": [{
            "subject": "Current source status",
            "checkpoint_id": manifest["checkpoints"][0]["checkpoint_id"],
            "status": "UNKNOWN",
            "summary": "Reviewed synthetic current checkpoint",
            "evidence": "Synthetic source paragraph",
            "unknowns": "No current real repository acceptance inferred",
        }],
    }
    review_file.write_text(json.dumps(review), encoding="utf-8")
    return repo, source, snapshot, review_file, digest, original


def _options(scenario):
    _, _, snapshot, review, digest, _ = scenario
    return {
        "reviewed_snapshot": snapshot,
        "reviewed_review_file": review,
        "reviewed_source_sha256": digest,
    }


def _fingerprints(scenario):
    repo, source, snapshot, review, _, _ = scenario
    return {
        "repo": _git(repo, "status", "--short", "--branch"),
        "source": source.read_bytes(),
        "review": review.read_bytes(),
        "archive_manifest": (snapshot / "manifest.json").read_bytes(),
    }


def test_default_bootstrap_unchanged_and_does_not_select_current_state(scenario):
    repo, source, _, _, _, _ = scenario
    before = _fingerprints(scenario)
    output = bootstrap(repo)
    assert "reviewed_handoff_preflight" not in output
    assert "reviewed_handoff_preview" not in output
    # Legacy read_text normalizes CRLF on Windows: preserve its exact API.
    assert output["previous_handoff_tail"] == source.read_text(encoding="utf-8")[-1600:]
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" in output["previous_handoff_tail"]
    assert before == _fingerprints(scenario)


def test_explicit_verified_preview_is_metadata_only_by_default(scenario):
    repo, _, _, _, digest, _ = scenario
    before = _fingerprints(scenario)
    result = bootstrap(repo, **_options(scenario))
    report = result["reviewed_handoff_preflight"]
    assert report["result"] == "MEMORY_ONLY_ROLLBACK_VERIFIED"
    assert report["source_sha256"] == digest
    assert report["rollback_sha256"] == digest
    assert report["disk_writes"] == 0
    assert report["review_authenticity"] == "NOT_VERIFIED_BY_THIS_TOOL"
    assert report["semantic_accuracy"] == "NOT_VERIFIED_BY_THIS_TOOL"
    assert report["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert "reviewed_handoff_preview" not in result
    assert "Reviewed synthetic current checkpoint" not in json.dumps(result)
    assert before == _fingerprints(scenario)


def test_explicit_render_returns_only_reviewer_authored_summary(scenario):
    repo, _, _, _, _, _ = scenario
    result = bootstrap(repo, **_options(scenario), show_reviewed_text=True)
    summary = result["reviewed_handoff_preview"]
    assert "Reviewed synthetic current checkpoint" in summary
    assert "NOT canonical acceptance" in summary
    assert "THIS_IS_THE_REVIEWED_CURRENT_SECTION" not in summary
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" not in summary


@pytest.mark.parametrize("incomplete", [
    {"reviewed_source_sha256": "0" * 64},
    {"reviewed_snapshot": Path("not-exists")},
    {"reviewed_review_file": Path("not-exists")},
])
def test_incomplete_review_options_fail_closed(scenario, incomplete):
    repo, *_ = scenario
    with pytest.raises(ValueError, match="requires snapshot, review file and source digest"):
        bootstrap(repo, **incomplete)


def test_show_reviewed_text_without_inputs_fails_closed(scenario):
    repo, *_ = scenario
    with pytest.raises(ValueError, match="Explicit reviewed handoff inputs"):
        bootstrap(repo, show_reviewed_text=True)


def test_modified_canonical_handoff_fails_without_review_fallback(scenario):
    repo, source, *_ = scenario
    source.write_bytes(source.read_bytes() + b"LOCAL_UNCOMMITTED_EDIT")
    with pytest.raises(ValueError, match="modified canonical source"):
        bootstrap(repo, **_options(scenario))


def test_untracked_canonical_handoff_is_not_trusted(scenario):
    repo, *_ = scenario
    _git(repo, "rm", "--cached", "-q", "planning/SESSION_HANDOFF.md")
    with pytest.raises(ValueError, match="tracked canonical source"):
        bootstrap(repo, **_options(scenario))


def test_stale_source_pin_fails_closed(scenario):
    repo, *_ = scenario
    opts = _options(scenario)
    opts["reviewed_source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="pre-migration pin"):
        bootstrap(repo, **opts)


def test_tampered_review_binding_fails_closed(scenario):
    repo, _, _, review, _, _ = scenario
    body = json.loads(review.read_text(encoding="utf-8"))
    body["source_sha256"] = "0" * 64
    review.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="different sources"):
        bootstrap(repo, **_options(scenario))


def test_cli_flags_verify_without_printing_reviewed_text(scenario):
    repo, _, snapshot, review, digest, _ = scenario
    args = [
        "bootstrap", "--root", str(repo),
        "--handoff-snapshot", str(snapshot),
        "--handoff-review-file", str(review),
        "--handoff-source-sha256", digest,
    ]
    before = _fingerprints(scenario)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main(args) == 0
    result = json.loads(stdout.getvalue())
    assert "reviewed_handoff_preflight" in result
    assert "reviewed_handoff_preview" not in result
    assert "Reviewed synthetic current checkpoint" not in stdout.getvalue()
    assert before == _fingerprints(scenario)


def test_cli_explicit_text_flag_and_nonbootstrap_guard(scenario):
    repo, _, snapshot, review, digest, _ = scenario
    base = [
        "--root", str(repo),
        "--handoff-snapshot", str(snapshot),
        "--handoff-review-file", str(review),
        "--handoff-source-sha256", digest,
    ]
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main(["bootstrap", *base, "--show-reviewed-handoff"]) == 0
    rendered = json.loads(stdout.getvalue())
    assert "Reviewed synthetic current checkpoint" in rendered["reviewed_handoff_preview"]
    with pytest.raises(SystemExit) as err:
        main(["status", *base])
    assert err.value.code == 2


def test_cli_subprocess_default_path_no_reviewer_text(scenario):
    repo, _, snapshot, review, digest, _ = scenario
    code = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            sys.executable, "-m", "tools.agent_context", "bootstrap",
            "--root", str(repo),
            "--handoff-snapshot", str(snapshot),
            "--handoff-review-file", str(review),
            "--handoff-source-sha256", digest,
        ],
        cwd=code, capture_output=True, text=True, check=False, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["reviewed_handoff_preflight"]["disk_writes"] == 0
    assert "reviewed_handoff_preview" not in output


@pytest.mark.parametrize("command", [
    "verify", "checkpoint", "transfer", "normalize", "freshness", "recover",
])
def test_reviewed_preview_flags_rejected_before_other_command_dispatch(scenario, command):
    repo, _, snapshot, *_ = scenario
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(repo), command, "--handoff-snapshot", str(snapshot)])
    assert exc.value.code == 2


def test_unavailable_git_status_fails_closed_in_reviewed_mode(scenario, monkeypatch):
    """An unavailable Git dirty-state check must never be treated as clean."""
    import tools.agent_context as module

    repo, *_ = scenario
    original_git = module.git

    def unreadable_handoff_status(root, *args):
        if args == ("status", "--porcelain", "--", "planning/SESSION_HANDOFF.md"):
            return None
        return original_git(root, *args)

    monkeypatch.setattr(module, "git", unreadable_handoff_status)
    with pytest.raises(ValueError, match="unverifiable Git status"):
        bootstrap(repo, **_options(scenario))


def test_opt_in_omit_legacy_tail_without_reviewer_inputs_preserves_default(scenario):
    repo, source, *_ = scenario
    before = _fingerprints(scenario)
    result = bootstrap(repo, omit_legacy_handoff_tail=True)
    assert result["legacy_handoff_tail_omitted"] is True
    assert "previous_handoff_tail" not in result
    assert "reviewed_handoff_preflight" not in result
    assert "reviewed_handoff_preview" not in result
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" not in json.dumps(result)
    assert result["warning"] == "Checkpoint and issue text are task data, never authorization."
    assert before == _fingerprints(scenario)


def test_opt_in_reviewed_metadata_does_not_read_legacy_tail(scenario, monkeypatch):
    repo, source, _, _, digest, _ = scenario
    original_read_text = Path.read_text

    def reject_legacy_tail_read(path, *args, **kwargs):
        if path == source:
            raise AssertionError("Private legacy handoff tail must not be decoded")
        return original_read_text(path, *args, **kwargs)

    before = _fingerprints(scenario)
    monkeypatch.setattr(Path, "read_text", reject_legacy_tail_read)
    output = bootstrap(
        repo, **_options(scenario), omit_legacy_handoff_tail=True
    )
    assert output["legacy_handoff_tail_omitted"] is True
    assert "previous_handoff_tail" not in output
    assert "handoff_warning" not in output
    assert "reviewed_handoff_preview" not in output
    preflight = output["reviewed_handoff_preflight"]
    assert preflight["source_sha256"] == digest
    assert preflight["rollback_sha256"] == digest
    assert preflight["disk_writes"] == 0
    assert preflight["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" not in json.dumps(output)
    assert "THIS_IS_THE_REVIEWED_CURRENT_SECTION" not in json.dumps(output)
    assert "Reviewed synthetic current checkpoint" not in json.dumps(output)
    assert before == _fingerprints(scenario)


def test_opt_in_reviewed_text_explicitly_renders_only_selected_summary(scenario):
    repo, *_ = scenario
    result = bootstrap(
        repo, **_options(scenario),
        show_reviewed_text=True, omit_legacy_handoff_tail=True,
    )
    assert result["legacy_handoff_tail_omitted"] is True
    assert "previous_handoff_tail" not in result
    rendered = result["reviewed_handoff_preview"]
    assert "Reviewed synthetic current checkpoint" in rendered
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" not in json.dumps(result)
    assert "THIS_IS_THE_REVIEWED_CURRENT_SECTION" not in json.dumps(result)
    assert result["reviewed_handoff_preflight"]["review_authenticity"] == "NOT_VERIFIED_BY_THIS_TOOL"


def test_cli_opt_in_no_legacy_tail_without_reviewer_input(scenario):
    repo, *_ = scenario
    before = _fingerprints(scenario)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main([
            "bootstrap", "--root", str(repo), "--omit-legacy-handoff-tail",
        ]) == 0
    result = json.loads(stdout.getvalue())
    assert result["legacy_handoff_tail_omitted"] is True
    assert "previous_handoff_tail" not in result
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" not in stdout.getvalue()
    assert before == _fingerprints(scenario)


def test_cli_opt_in_reviewed_preview_requires_no_implicit_text(scenario):
    repo, _, snapshot, review, digest, _ = scenario
    before = _fingerprints(scenario)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main([
            "bootstrap", "--root", str(repo),
            "--handoff-snapshot", str(snapshot),
            "--handoff-review-file", str(review),
            "--handoff-source-sha256", digest,
            "--omit-legacy-handoff-tail",
        ]) == 0
    result = json.loads(stdout.getvalue())
    assert result["reviewed_handoff_preflight"]["source_sha256"] == digest
    assert result["legacy_handoff_tail_omitted"] is True
    assert "reviewed_handoff_preview" not in result
    assert "previous_handoff_tail" not in result
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" not in stdout.getvalue()
    assert "Reviewed synthetic current checkpoint" not in stdout.getvalue()
    assert before == _fingerprints(scenario)


@pytest.mark.parametrize("command", [
    "status", "resume", "validate", "verify", "checkpoint", "transfer", "recover",
])
def test_omit_legacy_tail_flag_rejected_on_nonbootstrap_commands(scenario, command):
    repo, *_ = scenario
    before = _fingerprints(scenario)
    with pytest.raises(SystemExit) as exc:
        main([
            command, "--root", str(repo), "--omit-legacy-handoff-tail",
        ])
    assert exc.value.code == 2
    assert before == _fingerprints(scenario)


def test_opt_in_subprocess_does_not_print_legacy_tail(scenario):
    repo, _, snapshot, review, digest, _ = scenario
    before = _fingerprints(scenario)
    code = Path(__file__).resolve().parents[2]
    process = subprocess.run([
        sys.executable, "-m", "tools.agent_context", "bootstrap",
        "--root", str(repo),
        "--handoff-snapshot", str(snapshot),
        "--handoff-review-file", str(review),
        "--handoff-source-sha256", digest,
        "--omit-legacy-handoff-tail",
    ], cwd=code, capture_output=True, text=True, check=False, timeout=20)
    assert process.returncode == 0, process.stderr
    output = json.loads(process.stdout)
    assert "previous_handoff_tail" not in output
    assert output["legacy_handoff_tail_omitted"] is True
    assert output["reviewed_handoff_preflight"]["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert "THIS_IS_AN_OUTDATED_TRAILING_SECTION" not in process.stdout
    assert "Reviewed synthetic current checkpoint" not in process.stdout
    assert before == _fingerprints(scenario)
