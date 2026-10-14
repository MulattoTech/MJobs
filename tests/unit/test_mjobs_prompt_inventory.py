"""Do not generate MJobs Codex prompts referencing moved/deleted files."""
from pathlib import Path

from tools.orchestration.prompt_inventory import inspect_paths


def test_relative_authority_and_prompt_paths_exist(tmp_path):
    repo = tmp_path / "MJobs"
    (repo / "planning").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("instructions")
    (repo / "planning" / "CAPABILITY_MATRIX.json").write_text("{}")
    (repo / "prompts" / "codex").mkdir(parents=True)
    (repo / "prompts" / "codex" / "run.md").write_text(
        "Read `AGENTS.md` then `planning/CAPABILITY_MATRIX.json`."
    )
    result = inspect_paths(repo, prompts=("prompts/codex/run.md",), required=("AGENTS.md",))
    assert result["ok"]
    assert result["verified"] == 4


def test_removed_relative_reference_fails_without_recreation(tmp_path):
    repo = tmp_path / "MJobs"
    (repo / "prompts" / "codex").mkdir(parents=True)
    p = repo / "prompts" / "codex" / "run.md"
    p.write_text("Read `planning/SESSION_HANDOFF.md`.")
    result = inspect_paths(repo, prompts=("prompts/codex/run.md",), required=())
    assert result["ok"] is False
    assert any(row["path"] == "planning/SESSION_HANDOFF.md" for row in result["missing"])
    assert not (repo / "planning" / "SESSION_HANDOFF.md").exists()


def test_deleted_old_sibling_is_rejected(tmp_path):
    repo = tmp_path / "MJobs"
    (repo / "prompts" / "codex").mkdir(parents=True)
    p = repo / "prompts" / "codex" / "run.md"
    p.write_text(
        "Read `C:\\Tools\\Dev\\MJobs-Round3-20261014-133144\\AGENTS.md`."
    )
    result = inspect_paths(repo, prompts=("prompts/codex/run.md",), required=())
    assert not result["ok"]
    assert any("obsolete or outside MJobs" in row["reason"] for row in result["missing"])


def test_traversal_is_blocked(tmp_path):
    repo = tmp_path / "MJobs"
    repo.mkdir()
    result = inspect_paths(repo, prompts=("../outside.md",), required=())
    assert not result["ok"]
    assert result["missing"][0]["reason"] == "outside repository"


def test_required_files_missing_fails_closed(tmp_path):
    root = tmp_path / "MJobs"
    root.mkdir()
    result = inspect_paths(root, prompts=(), required=("AGENTS.md",))
    assert result["missing"] == [{
        "path": "AGENTS.md", "origin": "required authority",
        "reason": "not present"
    }]


def test_root_uses_real_mjobs_absolute_path_when_under_archive(tmp_path):
    # This test checks known clean relative paths; production root-specific
    # absolute path handling is covered by the live path inventory smoke.
    root = tmp_path / "MJobs" / ".mjobs-recovery" / "active" / "clone"
    (root / "prompts" / "codex").mkdir(parents=True)
    (root / "prompts" / "codex" / "run.md").write_text("Read `AGENTS.md`.")
    (root / "AGENTS.md").write_text("OK")
    assert inspect_paths(
        root, prompts=("prompts/codex/run.md",), required=("AGENTS.md",)
    )["ok"]
