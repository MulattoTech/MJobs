"""Exercise provenance against actual commits and Git's trailer boundaries."""
import subprocess

import pytest

from tools.verification.commit_provenance import validate


VALID_TRAILERS = """AI-Agent: Codex
AI-Model: gpt-6.1-sol
Requirement: MJ-REQ-288
Validation: synthetic fixture; hosted NOT_RUN
UTC-Date: 2026-10-17T04:17:02Z
"""

# The final constituent-commit section of protected squash ef6b629f. Earlier
# constituent messages also contain metadata, but cannot repair its footer.
MALFORMED_SQUASH = """[MJ-REQ-122] Harden provider normalization and review contracts (#695)

* fix(applications): preserve optional option closing semantics

Keep incomplete-form review fail-closed while accepting HTML with an omitted final option end tag.

AI-Agent: Codex

AI-Model: gpt-6.1-sol

Requirement: MJ-REQ-122 / #123

Validation: parser/provider unit tests 63 passed + 36 subtests; git diff --check; hosted/browser NOT_RUN

UTC-Date: 2026-10-17T04:17:02Z
"""


@pytest.fixture
def commit_source(tmp_path):
    repo = tmp_path / "repository"
    empty = tmp_path / "empty-hooks-and-template"
    empty.mkdir()

    def git(*args, input=None):
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Fixture Developer",
             "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
             "-c", f"core.hooksPath={empty}", *args],
            input=input, capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout

    repo.mkdir()
    git("init", f"--template={empty}", "--initial-branch=fixture")

    def commit(message):
        (repo / "example.py").write_text("value = 1\n", encoding="utf-8")
        git("add", "--", "example.py")
        git("commit", "--cleanup=verbatim", "--file=-", input=message)
        return repo, git("rev-parse", "HEAD").strip()

    return commit


@pytest.mark.parametrize("strict", [False, True])
def test_actual_squash_format_cannot_count_body_metadata_as_trailers(commit_source, strict):
    repo, revision = commit_source(MALFORMED_SQUASH)
    success, message = validate(repo, revision, strict=strict)
    assert success is False
    for key in ("AI-Agent", "AI-Model", "Requirement", "Validation"):
        assert key in message


@pytest.mark.parametrize("strict", [False, True])
def test_contiguous_final_attribution_block_is_accepted(commit_source, strict):
    repo, revision = commit_source("test: valid provenance\n\n" + VALID_TRAILERS)
    success, message = validate(repo, revision, strict=strict)
    assert success is True, message
    assert message.startswith("PASS:")


def test_body_ai_marker_does_not_become_human_exemption(commit_source):
    repo, revision = commit_source(
        "test: incomplete attribution\n\nAI-Agent: Codex\n\n"
        "This paragraph separates the body marker from the footer.\n\n"
        "UTC-Date: 2026-10-17T04:17:02Z\n"
    )
    success, message = validate(repo, revision)
    assert success is False
    assert "AI-Agent" in message


def test_valid_body_fields_cannot_backfill_incomplete_final_block(commit_source):
    repo, revision = commit_source(
        "test: incomplete footer\n\n" + VALID_TRAILERS
        + "\nThe preceding metadata is quoted body content.\n\n"
        "AI-Agent: Codex\nUTC-Date: 2026-10-17T04:17:02Z\n"
    )
    success, message = validate(repo, revision)
    assert success is False
    for key in ("AI-Model", "Requirement", "Validation"):
        assert key in message


@pytest.mark.parametrize("strict,expected", [(False, True), (True, False)])
def test_unmarked_human_source_retains_existing_strict_policy(commit_source, strict, expected):
    repo, revision = commit_source("test: ordinary human change\n")
    success, _ = validate(repo, revision, strict=strict)
    assert success is expected


def test_valid_trailer_boundary_still_rejects_non_utc_date(commit_source):
    message = "test: invalid date\n\n" + VALID_TRAILERS.replace(
        "2026-10-17T04:17:02Z", "2026-10-17T04:17:02-05:00",
    )
    repo, revision = commit_source(message)
    success, reason = validate(repo, revision, strict=True)
    assert success is False
    assert "UTC-Date" in reason
