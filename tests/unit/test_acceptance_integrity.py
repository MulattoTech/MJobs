"""Acceptance census must expose uncertainty instead of manufacturing proof."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from tools.verification import acceptance_integrity as audit


# Exact public issue175 dated context captured2026-10-18; never acceptance proof.
ISSUE175_PREFIX = (
    '## Current implementation — 2026-10-12\n\n[PR #586](https://github.com/MulattoTech/MJobs/p'
    'ull/586), current candidate `ae04e3704c0e60e36ad4223d8be12e917ba0218c`, separates runtim'
    'e PostgreSQL credentials from the preserved bootstrap administrator. API/scheduler/four '
    'workers receive only a read-only generated URL file; migrations and backup/restore retai'
    'n the original administrator. Provisioning works after migration on fresh/populated data'
    'bases, grants application DML/sequences/locks, excludes migration metadata and persisten'
    't/temporary DDL, and fails closed for privileged or mismatched existing roles. An indepe'
    'ndent versioned HMAC credential can be reconstructed from original protected configurati'
    'on; no `.env`, administrator password, encryption key or personal volume was changed.\n\nR'
    'endered-Compose regression RED demonstrated all six services receiving the administrator'
    ' URL. Local focused/adjacent62tests+77subtests PASS; syntax/catalogue/progress/dashboard'
    '/diff/provenance PASS. Fresh local Luna review identified inherited PUBLIC TEMP privileg'
    'e; explicit revocation and live negative assertion added. PostgreSQL permission tests re'
    'quire SQLSTATE42501 under autocommit, avoiding a false pass from CREATE DATABASE transac'
    'tion restrictions. Actual hosted integration/Compose checks are still running on the exa'
    'ct candidate; **whole requirement NOT_RUN/PARTIAL, not PASS**. No ORM/query acceptance i'
    'nferred from this single correction.\n\nThe disposable PostgreSQL test covers persisted pr'
    'ofile/ciphertext/document-hash preservation and original-key decryption, repeated provis'
    'ioning, DML/sequences/locks, denied privilege operations, future migration grants, missi'
    'ng generated-file recovery and wrong/overprivileged existing-role rejection. Local Docke'
    'r daemon is unavailable, so this live acceptance has not been claimed locally. Real exte'
    'rnal submissions remain OFF.\n\n---\n\n'
)


def issue175_seed():
    root = Path(__file__).resolve().parents[2]
    return next(json.loads(line)["body"] for line in
                (root / "planning/ISSUE_SEED.jsonl").read_text(encoding="utf-8-sig").splitlines()
                if json.loads(line)["id"] == "MJ-REQ-174")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", ISSUE175_PREFIX])
def test_seed_contract_matches_exact_or_dated_context_with_crlf(newline, prefix):
    seed = issue175_seed()
    body = (prefix + seed).replace("\r\n", "\n").replace("\n", newline)
    result = audit.compare_issue_contract(seed, body, "MJ-REQ-174")
    assert result["matches"] is True
    assert result["basis"] == ("EXACT_SEED" if not prefix else "SEED_WITH_DATED_CONTEXT")
    assert result["issue_body_sha256"] == hashlib.sha256(body.encode()).hexdigest()


@pytest.mark.parametrize("change", [
    lambda s: s.replace("least-privileged credentials", "administrator credentials"),
    lambda s: s.replace("- If blocked, classify the blocker rather than weakening the requirement.\n", ""),
    lambda s: s.replace("MJ-REQ-174", "MJ-REQ-175", 1),
    lambda s: s.replace("Requirement-ID: `MJ-REQ-174`", "Requirement-ID: `MJ-REQ-175`"),
    lambda s: s.replace("- Priority: `P1`", "- Priority: `P0`"),
    lambda s: s.replace("lines 5059-5072", "lines 1-2"),
    lambda s: s.replace("## Delivery contract\n", ""),
    lambda s: s.replace("## Delivery contract\n", "## Delivery contract\n## Delivery contract\n"),
    lambda s: s.replace("<!-- mj-requirement:MJ-REQ-174 -->\n", ""),
    lambda s: "<!-- mj-requirement:MJ-REQ-174 -->\n" + s,
    lambda s: s + "\n## Authoritative requirement text\nReplacement contract\n",
    lambda s: s + "\nRequirement-ID: `MJ-REQ-174`\n",
])
def test_changed_missing_duplicate_contract_content_fails_closed(change):
    seed = issue175_seed()
    assert audit.compare_issue_contract(seed, ISSUE175_PREFIX + change(seed), "MJ-REQ-174")["matches"] is False


@pytest.mark.parametrize("prefix", [
    "Undated arbitrary preface\n\n",
    ISSUE175_PREFIX.replace("2026-10-12", "2026-99-99"),
    ISSUE175_PREFIX + "## Requirement\nAmbiguous replacement\n\n",
    ISSUE175_PREFIX + "<!-- mj-requirement:MJ-REQ-175 -->\n",
    ISSUE175_PREFIX + "<!-- MJ-REQUIREMENT:MJ-REQ-174 -->\n",
    ISSUE175_PREFIX + "Requirement-ID: `MJ-REQ-174`\n",
    ISSUE175_PREFIX + "```text\n",
    ISSUE175_PREFIX + "~~~text\n",
    ISSUE175_PREFIX + "<!-- hidden contract\n",
    ISSUE175_PREFIX + "<details>\n",
    ISSUE175_PREFIX + "> quoted contract\n",
    ISSUE175_PREFIX.replace("\n---\n", "\n"),
])
def test_unrecognized_or_ambiguous_preface_cannot_hide_drift(prefix):
    seed = issue175_seed()
    assert audit.compare_issue_contract(seed, prefix + seed, "MJ-REQ-174")["matches"] is False


def test_invalid_seed_or_missing_issue_structure_never_matches_itself():
    seed = issue175_seed()
    for broken in ("", seed.replace("## Delivery contract\n", ""),
                   seed.replace("<!-- mj-requirement:MJ-REQ-174 -->", "")):
        assert audit.compare_issue_contract(broken, broken, "MJ-REQ-174")["matches"] is False
    assert audit.compare_issue_contract(seed, None, "MJ-REQ-174")["matches"] is False


def test_all366_original_seed_shapes_keep_exact_matching_without_promotion():
    root = Path(__file__).resolve().parents[2]
    seeds = [json.loads(line) for line in (root / "planning/ISSUE_SEED.jsonl").read_text(
        encoding="utf-8-sig").splitlines()]
    assert len(seeds) == 366
    for seed in seeds:
        comparison = audit.compare_issue_contract(seed["body"], seed["body"], seed["id"])
        assert comparison["matches"] is True, seed["id"]
        assert comparison["basis"] == "EXACT_SEED"


def page(nodes, total, more=False, cursor="one"):
    return {"data": {"user": {"projectV2": {"items": {
        "totalCount": total, "nodes": nodes,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
    }}}}}


def item(number=293, repository="MulattoTech/MJobs"):
    return {"id": str(number), "content": {"number": number,
            "repository": {"nameWithOwner": repository}},
            "fieldValues": {"nodes": [
                {"name": "Done", "field": {"name": "Status"}},
                {"name": "PARTIAL", "field": {"name": "Acceptance"}},
                {"name": "P0", "field": {"name": "Priority"}},
            ], "pageInfo": {"hasNextPage": False}}}


def test_full_pagination_keeps_only_target_repository():
    result = audit.project_items([page([item()], 2, True),
                                  page([item(294, "someone/else")], 2, cursor="two")])
    assert list(result) == [293]


@pytest.mark.parametrize("pages", [
    [page([item()], 2, True)],
    [page([item()], 2)],
    [page([item()], 2, True), page([item()], 2, cursor="two")],
    [page([item()], 1, True), page([], 1, cursor="two")],
])
def test_partial_or_repeated_project_pages_cannot_claim_complete_census(pages):
    with pytest.raises(ValueError):
        audit.project_items(pages)


def test_truncated_project_fields_cannot_silently_hide_acceptance():
    value = item()
    value["fieldValues"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(ValueError):
        audit.project_items([page([value], 1)])


def test_closed_slice_does_not_complete_partial_parent():
    flags = audit.lifecycle_flags(
        {"status": "PARTIAL", "priority": "P0"},
        {"state": "closed", "state_reason": "completed"},
        {"Status": "Done", "Acceptance": "PARTIAL", "Priority": "P0"})
    assert flags == ["CLOSED_WITHOUT_PASS", "PROJECT_DONE_WITHOUT_PASS"]


def test_historical_hash_drift_and_ci_link_never_become_current_proof():
    old = hashlib.sha256(b"old test").hexdigest()
    evidence = {"source_sha256": {"tests/unit/test_example.py": old},
                "source_candidate": "a" * 40,
                "ci": "https://github.com/MulattoTech/MJobs/actions/runs/123"}
    result = audit.evidence_identity(evidence, {"tests/unit/test_example.py": b"changed test"})
    assert result["source_hashes"] == {"matched": 0, "drifted": 1, "missing": 0}
    assert result["tests"][0]["path"] == "tests/unit/test_example.py"
    assert result["tests"][0]["current_sha256"] != old
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"
    assert result["check_urls"] == [evidence["ci"]]


def test_matching_test_bytes_still_do_not_prove_execution_or_full_scope():
    digest = hashlib.sha256(b"test").hexdigest()
    result = audit.evidence_identity(
        {"files": [{"path": "tests/unit/test_example.py", "sha256": digest}]},
        {"tests/unit/test_example.py": b"test"})
    assert result["source_hashes"]["matched"] == 1
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"


def test_missing_hash_target_remains_missing():
    result = audit.evidence_identity({"source_sha256": {"tests/absent.py": "a" * 64}}, {})
    assert result["source_hashes"]["missing"] == 1


def test_retained_ci370_uppercase_digests_match_all_three_git_artifacts():
    root = Path(__file__).resolve().parents[2]
    document = audit.read_evidence(root, "docs/evidence/2026-09-30-ci-370.json")
    before = json.dumps(document, sort_keys=True)
    sources = {row["path"]: subprocess.check_output(["git", "show", f"HEAD:{row['path']}"], cwd=root)
               for row in document["artifacts"]}
    assert len(sources) == 3
    assert all(hashlib.sha256(sources[row["path"]]).hexdigest() == row["sha256"].lower()
               for row in document["artifacts"])
    result = audit.evidence_identity(document, sources)
    assert result["source_hashes"] == {"matched": 3, "drifted": 0, "missing": 0}
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"
    assert json.dumps(document, sort_keys=True) == before


@pytest.mark.parametrize("case", ["lower", "upper", "mixed"])
@pytest.mark.parametrize("shape", ["path_record", "path_mapping"])
def test_hex_digest_case_is_normalized_without_mutating_evidence(case, shape):
    path = "tests/unit/test_example.py"
    digest = hashlib.sha256(b"historical bytes").hexdigest()
    recorded = digest if case == "lower" else digest.upper() if case == "upper" else "".join(
        c.upper() if i % 2 else c for i, c in enumerate(digest))
    document = {"files": [{"path": path, "sha256": recorded}]} if shape == "path_record" else {
        "source_sha256": {path: recorded}}
    before = json.dumps(document, sort_keys=True)
    result = audit.evidence_identity(document, {path: b"historical bytes"})
    assert result["source_hashes"] == {"matched": 1, "drifted": 0, "missing": 0}
    assert result["tests"] == [{"path": path, "recorded_sha256": [digest], "current_sha256": digest}]
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"
    assert json.dumps(document, sort_keys=True) == before


def test_equivalent_digest_cases_deduplicate_but_different_digests_keep_drift():
    path = "tests/unit/test_example.py"
    digest = hashlib.sha256(b"test").hexdigest()
    other = hashlib.sha256(b"other").hexdigest()
    document = {"files": [{"path": path, "sha256": digest.upper()},
                          {"path": path, "sha256": other.upper()}],
                "source_sha256": {path: digest}}
    result = audit.evidence_identity(document, {path: b"test"})
    assert result["source_hashes"] == {"matched": 1, "drifted": 1, "missing": 0}
    assert result["tests"][0]["recorded_sha256"] == sorted([digest, other])
    assert result["changed_or_missing_paths"] == [path]
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 65, "G" * 64, "A" * 63 + "z",
    " " + "a" * 64, "a" * 64 + "\n", "0x" + "a" * 64, None, 1, int("1" * 64), ["a" * 64], "\uff21" * 64])
@pytest.mark.parametrize("shape", ["path_record", "path_mapping"])
def test_malformed_digest_never_becomes_a_source_identity(digest, shape):
    path = "services/example.py"
    document = {"files": [{"path": path, "sha256": digest}]} if shape == "path_record" else {
        "source_sha256": {path: digest}}
    result = audit.evidence_identity(document, {path: b"test"})
    assert result["source_hashes"] == {"matched": 0, "drifted": 0, "missing": 0}
    assert result["changed_or_missing_paths"] == []
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"


def test_uppercase_digest_missing_target_remains_missing_not_pass():
    result = audit.evidence_identity({"source_sha256": {"tests/absent.py": "A" * 64}}, {})
    assert result["source_hashes"] == {"matched": 0, "drifted": 0, "missing": 1}
    assert result["tests"][0]["recorded_sha256"] == ["a" * 64]
    assert result["tests"][0]["current_sha256"] is None
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"


def test_markdown_evidence_is_supported_without_claiming_machine_verified_proof(tmp_path):
    (tmp_path / "evidence.md").write_text("Ran tests/unit/test_example.py; PASS", encoding="utf-8")
    document = audit.read_evidence(tmp_path, "evidence.md")
    result = audit.evidence_identity(document, {"tests/unit/test_example.py": b"test"})
    assert result["tests"][0]["path"] == "tests/unit/test_example.py"
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"


def test_path_escape_is_not_read_as_evidence(tmp_path):
    with pytest.raises(ValueError):
        audit.read_local_json(tmp_path, "../private.json")


def test_output_cannot_overwrite_canonical_planning(tmp_path):
    planning = tmp_path / "planning"
    planning.mkdir()
    target = planning / "CAPABILITY_MATRIX.json"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="planning"):
        audit.write_report(tmp_path, target, {})
    assert target.read_text(encoding="utf-8") == "original"


@pytest.fixture
def disposable_git_worktree(tmp_path):
    """All administrative-path reproductions stay inside this disposable tree."""
    repository, worktree = tmp_path / "repository", tmp_path / "worktree"
    def git(*args, cwd=repository):
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE).decode().strip()
    repository.mkdir()
    git("init")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "fixture")
    git("worktree", "add", "-b", "fixture-worktree", str(worktree))
    git_dir = Path(git("rev-parse", "--absolute-git-dir", cwd=worktree)).resolve()
    common_dir = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree)).resolve()
    assert git_dir.is_relative_to(tmp_path.resolve())
    assert common_dir.is_relative_to(tmp_path.resolve())
    return repository, worktree, git_dir, common_dir


@pytest.mark.parametrize("destination", ["marker", "git_dir", "common_dir", "ordinary_repo", "new_admin_file"])
def test_output_rejects_git_administration_before_write(disposable_git_worktree, destination):
    repository, worktree, git_dir, common_dir = disposable_git_worktree
    root = repository if destination == "ordinary_repo" else worktree
    target = {"marker": worktree / ".git", "git_dir": git_dir / "HEAD",
              "common_dir": common_dir / "config", "ordinary_repo": common_dir / "HEAD",
              "new_admin_file": git_dir / "unexpected-report.json"}[destination]
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(root, target, {"unsafe": True})
    assert (target.read_bytes() if target.exists() else None) == before
    subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True)


@pytest.mark.parametrize("destination", ["marker", "git_dir", "common_dir"])
def test_output_rejects_symlink_alias_to_git_metadata(disposable_git_worktree, tmp_path, destination):
    _, worktree, git_dir, common_dir = disposable_git_worktree
    target = {"marker": worktree / ".git", "git_dir": git_dir, "common_dir": common_dir}[destination]
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    output = alias if destination == "marker" else alias / "HEAD"
    before = output.read_bytes()
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(worktree, output, {})
    assert output.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive filesystem alias")
def test_output_rejects_windows_case_alias(disposable_git_worktree):
    _, worktree, _, _ = disposable_git_worktree
    marker = worktree / ".GiT"
    before = marker.read_bytes()
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(worktree, marker, {})
    assert marker.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
@pytest.mark.parametrize("destination", ["git_dir", "common_dir"])
def test_output_rejects_windows_junction_alias(disposable_git_worktree, tmp_path, destination):
    _, worktree, git_dir, common_dir = disposable_git_worktree
    target = git_dir if destination == "git_dir" else common_dir
    alias = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                   check=True, capture_output=True)
    output = alias / "HEAD"
    before = output.read_bytes()
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(worktree, output, {})
    assert output.read_bytes() == before


def test_output_allows_external_artifact_and_untracked_workspace_report(disposable_git_worktree, tmp_path):
    _, worktree, _, _ = disposable_git_worktree
    artifact_directory = tmp_path / "ao-artifacts"
    artifact_directory.mkdir()
    for target in (artifact_directory / "report.json", worktree / "report.json"):
        audit.write_report(worktree, target, {"count": 425})
        assert json.loads(target.read_text(encoding="utf-8")) == {"count": 425}


def test_lineage_does_not_confuse_parent_with_dotted_child_or_infer_acceptance():
    pulls = [{"number": 677, "title": "MJ-REQ-017.S01 slice", "body": "Refs #293",
              "merged_at": "2026-10-16", "head": {"sha": "a" * 40},
              "merge_commit_sha": "b" * 40, "html_url": "https://github.com/MulattoTech/MJobs/pull/677"}]
    assert audit.pr_lineage("MJ-REQ-017", 18, pulls) == []
    linked = audit.pr_lineage("MJ-REQ-292", 293, pulls)
    assert linked[0]["basis"] == "TEXT_REFERENCE_ONLY"
    assert linked[0]["head_sha"] == "a" * 40


def test_census_covers_all425_without_promoting_historical_results():
    from pathlib import Path
    seed = issue175_seed()
    provider_body = ISSUE175_PREFIX + seed.replace("\n", "\r\n")
    project_item = item(175)
    for field, value in zip(project_item["fieldValues"]["nodes"], ("Todo", "NOT_RUN", "P1")):
        field["name"] = value
    report = audit.build_census(Path(__file__).resolve().parents[2], {
        "issues": [{"number": 175, "body": provider_body, "state": "open", "labels": []}],
        "project_pages": [page([project_item], 1)], "pulls": [],
    })
    row = next(row for row in report["requirements"] if row["id"] == "MJ-REQ-174")
    assert "ISSUE_CONTRACT_DIFFERS" not in row["flags"]
    assert row["status"] == row["proposed_state"] == "NOT_RUN"
    assert len(report["requirements"]) == 425
    assert sum(report["summary"]["original"].values()) == 366
    assert sum(report["summary"]["supplemental"].values()) == 59
    assert len(report["baseline_p0_ids"]) == 26
    assert {r["fresh_acceptance"] for r in report["requirements"]} == {"NOT_RUN"}
    assert report["requirements"][0]["id"].startswith("MJ-")

@pytest.fixture
def owned_git_child(monkeypatch):
    """Replace only this test process's Git launch with finite owned children."""
    import sys
    real_popen = subprocess.Popen
    children = []
    behavior = {'mode': 'before'}
    oid = b'a' * 40

    def launch(args, *positional, **kwargs):
        if args[0] == 'git':
            if 'ls-tree' in args:
                script = "import sys;sys.stdout.buffer.write(" + repr(b'100644 blob ' + oid + b'\tfixture.py\0') + ")"
            elif '--batch' in args:
                script = "import sys,time;sys.stdin.buffer.read();"
                if behavior['mode'] == 'mid':
                    script += "sys.stdout.buffer.write(" + repr(oid + b' blob 10\npart') + ");sys.stdout.flush();"
                script += 'time.sleep(1)'
            else:
                script = 'import time;time.sleep(1)'
            args = [sys.executable, '-I', '-c', script]
        child = real_popen(args, *positional, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(audit.subprocess, 'Popen', launch)
    monkeypatch.setattr(audit, 'COMMAND_TIMEOUT_SECONDS', .2, raising=False)
    yield behavior, children
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


def assert_reaped_and_follow_on(children):
    import sys
    assert children and all(child.poll() is not None for child in children)
    assert all(stream is None or stream.closed for child in children
               for stream in (child.stdin, child.stdout, child.stderr))
    assert audit._run([sys.executable, '-I', '-c', "import sys;sys.stdout.buffer.write(b'healthy\\n')"]) == b'healthy\n'


def test_git_command_deadline_reaps_owned_child_and_allows_follow_on(owned_git_child, tmp_path):
    import time
    _, children = owned_git_child
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='COMMAND_TIMEOUT'):
        audit._run(['git', 'rev-parse', 'HEAD'], tmp_path)
    assert time.monotonic() - started < .8
    assert_reaped_and_follow_on(children)


@pytest.mark.parametrize('mode', ['before', 'mid'])
def test_git_batch_deadline_before_or_inside_response_fails_closed(owned_git_child, tmp_path, mode):
    import time
    behavior, children = owned_git_child
    behavior['mode'] = mode
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='COMMAND_TIMEOUT'):
        audit.git_sources(tmp_path, 'HEAD')
    assert time.monotonic() - started < .9
    assert_reaped_and_follow_on(children)


def test_command_timeout_never_replaces_previous_report(owned_git_child, tmp_path, monkeypatch):
    import sys
    output = tmp_path / 'prior-report.json'
    output.write_bytes(b'prior immutable evidence')
    monkeypatch.setattr(sys, 'argv', ['acceptance_integrity', '--root', str(tmp_path), '--output', str(output)])
    with pytest.raises(RuntimeError, match='COMMAND_TIMEOUT'):
        audit.main()
    assert output.read_bytes() == b'prior immutable evidence'

@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_command_output_cap_is_combined_and_reaps_flooding_child(owned_git_child, monkeypatch, stream):
    import sys
    _, children = owned_git_child
    monkeypatch.setattr(audit, 'MAX_COMMAND_OUTPUT_BYTES', 8192)
    script = f'import sys\nwhile True: sys.{stream}.buffer.write(b"x"*4096);sys.{stream}.flush()'
    with pytest.raises(audit.AuditCommandError, match='COMMAND_OUTPUT_LIMIT'):
        audit._run([sys.executable, '-I', '-c', script])
    assert_reaped_and_follow_on(children)


def test_stalled_child_cannot_block_batch_stdin_writer(owned_git_child):
    import sys
    _, children = owned_git_child
    with pytest.raises(audit.AuditCommandError, match='COMMAND_TIMEOUT'):
        audit._run([sys.executable, '-I', '-c', 'import time;time.sleep(1)'], input_data=b'x' * 1_000_000)
    assert_reaped_and_follow_on(children)


def test_stdout_stderr_and_input_share_one_bounded_exchange(owned_git_child, monkeypatch):
    import sys
    _, children = owned_git_child
    monkeypatch.setattr(audit, 'COMMAND_TIMEOUT_SECONDS', 2)
    payload = bytes(range(256)) * 1000
    script = ('import sys;sys.stderr.buffer.write(b"d"*70000);sys.stderr.flush();'
              'data=sys.stdin.buffer.read();sys.stdout.buffer.write(data)')
    assert audit._run([sys.executable, '-I', '-c', script], input_data=payload) == payload
    assert_reaped_and_follow_on(children)


def test_input_limit_rejects_before_any_child_launch(owned_git_child, monkeypatch):
    import sys
    _, children = owned_git_child
    monkeypatch.setattr(audit, 'MAX_COMMAND_INPUT_BYTES', 4)
    with pytest.raises(audit.AuditCommandError, match='COMMAND_INPUT_LIMIT'):
        audit._run([sys.executable, '-I', '-c', 'pass'], input_data=b'too long')
    assert children == []


def test_nonzero_exit_is_explicit_without_echoing_child_diagnostics(owned_git_child):
    import sys
    _, children = owned_git_child
    with pytest.raises(audit.AuditCommandError, match=r'COMMAND_FAILED \(exit=7\)') as error:
        audit._run([sys.executable, '-I', '-c', 'import sys;sys.stderr.write("fictional-private-diagnostic");sys.exit(7)'])
    assert 'fictional-private-diagnostic' not in str(error.value)
    assert_reaped_and_follow_on(children)


@pytest.mark.parametrize('response', [
    b'', b'a'*40 + b' blob 4\nab', b'a'*40 + b' blob 4\nabcd',
    b'a'*40 + b' blob -1\n\n', b'a'*40 + b' tree 0\n\n',
    b'b'*40 + b' blob 0\n\n', b'a'*40 + b' blob 0\n\nextra',
    b'a'*40 + b' blob nope\n', b'a'*40 + b' missing\n',
])
def test_incomplete_or_malformed_batch_is_never_hashed(response, monkeypatch, tmp_path):
    tree = b'100644 blob ' + b'a'*40 + b'\tfixture.bin\0'
    monkeypatch.setattr(audit, '_run', lambda args, root, **kw: tree if 'ls-tree' in args else response)
    with pytest.raises(audit.AuditCommandError, match='GIT_BATCH_MALFORMED'):
        audit.git_sources(tmp_path, 'HEAD')


@pytest.mark.parametrize('tree', [b'not a tree', b'100644 blob bad\tfile\0',
                                  b'100644 blob ' + b'a'*40 + b'\tfile'])
def test_malformed_tree_cannot_produce_partial_source_inventory(tree, monkeypatch, tmp_path):
    monkeypatch.setattr(audit, '_run', lambda *args, **kw: tree)
    with pytest.raises(audit.AuditCommandError, match='GIT_TREE_MALFORMED'):
        audit.git_sources(tmp_path, 'HEAD')


def test_complete_binary_batch_preserves_all_bytes_and_duplicate_object_paths(monkeypatch, tmp_path):
    oid = b'a'*40
    content = bytes(range(256)) * 300
    tree = b''.join(b'100644 blob ' + oid + b'\t' + p + b'\0' for p in [b'first.bin', b'second.bin'])
    response = oid + b' blob ' + str(len(content)).encode() + b'\n' + content + b'\n'
    def run(args, root, **kwargs):
        if 'ls-tree' in args:
            return tree
        assert kwargs['input_data'] == oid + b'\n'
        return response
    monkeypatch.setattr(audit, '_run', run)
    result = audit.git_sources(tmp_path, 'HEAD')
    assert result == {'first.bin': content, 'second.bin': content}
    assert hashlib.sha256(result['first.bin']).digest() == hashlib.sha256(content).digest()


def test_exited_child_with_owned_pipe_still_open_has_finite_deadline(monkeypatch):
    """An owned fixture writer keeps EOF away after the actual child exits."""
    import sys
    real_popen = subprocess.Popen
    read_fd, write_fd = os.pipe()
    children = []

    def launch(*args, **kwargs):
        child = real_popen([sys.executable, '-I', '-c', 'pass'], **kwargs)
        child.stdout.close()
        child.stdout = os.fdopen(read_fd, 'rb', buffering=0)
        children.append(child)
        return child

    monkeypatch.setattr(audit.subprocess, 'Popen', launch)
    monkeypatch.setattr(audit, 'COMMAND_TIMEOUT_SECONDS', .3)
    try:
        with pytest.raises(audit.AuditCommandError, match='COMMAND_TIMEOUT'):
            audit._run(['owned-git-like-pipe-fixture'])
        assert children[0].returncode == 0
        assert children[0].stdout.closed and children[0].stderr.closed
    finally:
        os.close(write_fd)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()
            child.stderr.close()
    monkeypatch.undo()
    assert audit._run([sys.executable, '-I', '-c', "import sys;sys.stdout.buffer.write(b'ok')"]) == b'ok'

@pytest.mark.parametrize('extra', [0, 1])
def test_combined_output_limit_accepts_exact_boundary_only(owned_git_child, monkeypatch, extra):
    import sys
    _, children = owned_git_child
    monkeypatch.setattr(audit, 'MAX_COMMAND_OUTPUT_BYTES', 8192)
    script = f'import sys;sys.stdout.buffer.write(b"o"*4096);sys.stdout.flush();sys.stderr.buffer.write(b"e"*{4096+extra});sys.stderr.flush()'
    if extra:
        with pytest.raises(audit.AuditCommandError, match='COMMAND_OUTPUT_LIMIT'):
            audit._run([sys.executable, '-I', '-c', script])
    else:
        assert audit._run([sys.executable, '-I', '-c', script]) == b'o'*4096
    assert_reaped_and_follow_on(children)
