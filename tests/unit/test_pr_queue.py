"""Deterministic publication-policy regressions; no GitHub writes or credentials."""
import json
from types import SimpleNamespace

import pytest

from tools.verification.pr_queue import assess, classify, github_pulls, main, validate_policy


POLICY = {"max_active_prs": 32, "capacity_behavior": "warn"}


def pull(number=1, **updates):
    result = {"number": number, "state": "open", "draft": False,
              "user": {"id": 5900632, "login": "MulattoTech", "type": "User"},
              "head": {"sha": f"{number:040x}", "ref": f"feature/{number}",
                       "repo": {"full_name": "MulattoTech/MJobs"}},
              "title": "Bump dependencies", "mergeable": None}
    result.update(updates)
    return result


def candidate():
    return {"head_sha": "f" * 40, "head_ref": "feature/new", "head_repo": "MulattoTech/MJobs", "work_id": "policy-32",
            "owner": "mjobs-17", "commit_count": 1, "changed_files": 3,
            "meaningful_change": True, "local_validation": ["pytest: PASS"],
            "nonduplicate_review": "Compared live open PR scope and owned paths"}


def test_dependency_identity_requires_bot_type_numeric_id_and_login():
    bot = {"id": 49699333, "login": "dependabot[bot]", "type": "Bot"}
    assert classify(pull(user=bot), {})["dependency_automation"]
    for key, value in [("id", 7), ("type", "User"), ("login", "dependabot-lookalike[bot]")]:
        fake = dict(bot, **{key: value})
        assert not classify(pull(user=fake), {})["dependency_automation"]
    assert not classify(pull(), {})["dependency_automation"]


def test_publisher_does_not_imply_authorship_and_review_is_head_bound():
    pr = pull()
    assert classify(pr, {})["authorship"] == "unknown"
    reviewed = {"1": {"head_sha": pr["head"]["sha"], "reviewed_by": "owner",
                      "review_reference": "review/1", "authorship": "AI",
                      "category": "security", "work_id": "security-1"}}
    assert classify(pr, reviewed)["authorship"] == "AI"
    assert classify(pr, reviewed)["category"] == "security"
    reviewed["1"]["authorship"] = "human"
    assert classify(pr, reviewed)["authorship"] == "human"
    reviewed["1"]["head_sha"] = "0" * 40
    assert classify(pr, reviewed)["category"] == "unknown"


@pytest.mark.parametrize("metadata", [None, [], {"1": []}, {"1": {"authorship": "AI"}}])
def test_malformed_metadata_is_unknown_and_counted(metadata):
    result = assess([pull(draft=True)], POLICY, metadata)
    assert result["development_open"] == result["drafts"] == 1
    assert result["authorship"] == {"unknown": 1}


def test_drafts_categories_and_dependency_exclusion_are_orthogonal():
    prs = [pull(1, draft=True), pull(2), pull(3, user={"id": 49699333,
            "login": "dependabot[bot]", "type": "Bot"}), pull(4, state="closed")]
    result = assess(prs, POLICY, {})
    assert result["development_open"] == 2
    assert result["dependency_open"] == 1
    assert result["drafts"] == 1
    assert result["categories"] == {"unknown": 2}
    assert result["conflicts"]["unknown"] == [1, 2]
    assert result["ci"] == "NOT_QUERIED"


def test_capacity_threshold_warns_at_32_and_hard_blocks_only_over_projected_limit():
    prs = [pull(i) for i in range(1, 33)]
    result = assess(prs, POLICY, {}, candidate())
    assert result["capacity"] == "WARNING"
    assert result["allowed"]
    hard = dict(POLICY, capacity_behavior="block")
    assert assess(prs[:31], hard, {}, candidate())["allowed"]
    assert not assess(prs, hard, {}, candidate())["allowed"]
    assert assess(prs[:30], POLICY, {}, candidate())["capacity"] == "OK"


@pytest.mark.parametrize("updates", [{"commit_count": 0}, {"changed_files": 0},
    {"meaningful_change": False}, {"owner": ""}, {"local_validation": []},
    {"nonduplicate_review": ""}, {"commit_count": True}, {"head_sha": "bad"}])
def test_capacity_warning_never_relaxes_publication_gates(updates):
    item = dict(candidate(), **updates)
    assert not assess([], POLICY, {}, item)["allowed"]


def test_duplicate_identity_and_reviewed_work_are_blocked():
    item = candidate()
    assert not assess([pull(head={"sha": item["head_sha"], "ref": "other"})], POLICY, {}, item)["allowed"]
    assert not assess([pull(head={"sha": "a" * 40, "ref": item["head_ref"]})], POLICY, {}, item)["allowed"]
    meta = {"1": {"head_sha": pull()["head"]["sha"], "reviewed_by": "owner",
                  "review_reference": "review/1", "work_id": item["work_id"]}}
    assert not assess([pull()], POLICY, meta, item)["allowed"]


def test_matching_branch_names_across_forks_are_not_duplicate_work():
    item = dict(candidate(), head_repo="bob/MJobs", head_ref="patch-1")
    pr = pull(head={"sha": "a" * 40, "ref": "patch-1", "repo": {"full_name": "alice/MJobs"}})
    assert assess([pr], POLICY, {}, item)["allowed"]


def test_matching_branch_in_same_repository_is_duplicate_case_insensitive_repo():
    item = dict(candidate(), head_repo="ALICE/mjobs", head_ref="patch-1")
    pr = pull(head={"sha": "a" * 40, "ref": "patch-1", "repo": {"full_name": "alice/MJobs"}})
    assert not assess([pr], POLICY, {}, item)["allowed"]


@pytest.mark.parametrize("source", [None, "", "not-qualified", [], "alice/MJobs/extra"])
def test_candidate_requires_qualified_source_repository(source):
    assert not assess([], POLICY, {}, dict(candidate(), head_repo=source))["allowed"]


def test_unknown_source_repository_cannot_clear_matching_branch():
    item = candidate()
    pr = pull(head={"sha": "a" * 40, "ref": item["head_ref"], "repo": None})
    result = assess([pr], POLICY, {}, item)
    assert not result["allowed"]
    assert any("unknown source repository" in error for error in result["errors"])


@pytest.mark.parametrize("prs", [[None], [pull(), pull()], [{"state": "open"}], {}])
def test_incomplete_or_duplicate_snapshot_cannot_authorize_publication(prs):
    assert not assess(prs, POLICY, {}, candidate())["allowed"]


@pytest.mark.parametrize("policy", [[], None, {"max_active_prs": 0}, {"max_active_prs": True},
    {"max_active_prs": 32, "capacity_behavior": "unlimited"},
    {"max_active_prs": 32, "capacity_behavior": []}])
def test_invalid_policy_fails_closed(policy):
    assert validate_policy(policy)
    assert not assess([], policy, {}, candidate())["allowed"]


@pytest.mark.parametrize("category", ["feature", "docs-maintenance", "security", "acceptance", "integration"])
def test_all_reviewed_categories_count_and_draft_is_orthogonal(category):
    pr = pull(draft=True)
    meta = {"1": {"head_sha": pr["head"]["sha"], "reviewed_by": "owner",
                   "review_reference": "review/1", "category": category,
                   "authorship": []}}
    result = assess([pr], POLICY, meta)
    assert result["categories"] == {category: 1}
    assert result["authorship"] == {"unknown": 1}
    assert result["drafts"] == result["development_open"] == 1


def test_legacy_cap_and_noempty_flags():
    assert not assess([pull()], {"max_active_prs": 1}, {}, candidate())["allowed"]
    assert validate_policy(dict(POLICY, never_create_empty_prs=False))


def test_live_query_paginates_fixed_authenticated_endpoint(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps([[pull()], [pull(2)]]))
    monkeypatch.setattr("tools.verification.pr_queue.subprocess.run", run)
    assert len(github_pulls()) == 2
    assert calls[0][0] == ["gh", "api", "--hostname", "github.com", "--paginate", "--slurp",
                          "repos/MulattoTech/MJobs/pulls?state=open&per_page=100"]
    assert calls[0][1]["check"] is True
    assert calls[0][1]["encoding"] == "utf-8"


def test_cli_rejects_null_candidate_instead_of_silently_running_inventory(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pr_queue", "--live", "--candidate", "null.json"])
    monkeypatch.setattr("pathlib.Path.read_text", lambda *args, **kwargs: "null")
    assert main() == 1
    assert json.loads(capsys.readouterr().out)["allowed"] is False


def test_catalogue_consumes_policy_validation():
    from pathlib import Path
    from unittest.mock import patch
    from tools.verification.catalogue import validate_catalogue
    with patch("tools.verification.catalogue.validate_policy", return_value=["policy sentinel"]) as validator:
        errors = validate_catalogue(Path(__file__).resolve().parents[2])
    validator.assert_called_once()
    assert "policy sentinel" in errors


def owned_update():
    """Reviewed operator input, independently bound to the old provider head."""
    pr = pull()
    pr["base"] = {"ref": "main", "repo": {"full_name": "MulattoTech/MJobs"}}
    item = dict(candidate(), head_ref=pr["head"]["ref"],
                previous_head_sha=pr["head"]["sha"], base_ref="main",
                base_repo="MulattoTech/MJobs")
    metadata = {"1": {"head_sha": pr["head"]["sha"], "reviewed_by": "integrator",
                      "review_reference": "owned-update-review/1", "owner": item["owner"],
                      "work_id": item["work_id"], "head_ref": item["head_ref"],
                      "head_repo": item["head_repo"], "base_ref": item["base_ref"],
                      "base_repo": item["base_repo"], "publisher_id": pr["user"]["id"],
                      "publisher_login": pr["user"]["login"]}}
    return pr, item, metadata


def test_existing_owned_update_reproduces_new_candidate_duplicate_rejection():
    pr, item, metadata = owned_update()
    result = assess([pr], POLICY, metadata, item)
    assert "duplicate open PR scope/identity: 1" in result["errors"]
    assert result["projected_development_open"] == 2


def test_explicit_owned_update_admits_same_branch_without_adding_capacity():
    pr, item, metadata = owned_update()
    result = assess([pr], {"max_active_prs": 1}, metadata, item, update_pr=1)
    assert result["allowed"], result["errors"]
    assert result["development_open"] == result["projected_development_open"] == 1
    assert result["backlog"] == [1]
    assert result["publication_mode"] == "update"
    assert result["validated_update_pr"] == 1
    assert result["publication_checked"]


@pytest.mark.parametrize("number", [True, 0, -1, "1", 2])
def test_update_requires_known_positive_integer_pr_number(number):
    pr, item, metadata = owned_update()
    assert not assess([pr], POLICY, metadata, item, update_pr=number)["allowed"]


@pytest.mark.parametrize("field,value", [
    ("owner", "other-session"), ("work_id", "different-work"),
    ("head_ref", "Feature/1"), ("head_repo", "external/MJobs"),
    ("base_ref", "release"), ("base_repo", "external/MJobs"),
    ("previous_head_sha", "a" * 40), ("previous_head_sha", None),
    ("head_sha", "1".zfill(40)), ("local_validation", []),
    ("meaningful_change", False), ("changed_files", 0),
])
def test_update_rejects_candidate_identity_drift_and_unqualified_change(field, value):
    pr, item, metadata = owned_update()
    item[field] = value
    assert not assess([pr], POLICY, metadata, item, update_pr=1)["allowed"]


@pytest.mark.parametrize("field", ["head_sha", "reviewed_by", "review_reference", "owner",
    "work_id", "head_ref", "head_repo", "base_ref", "base_repo", "publisher_id", "publisher_login"])
def test_update_requires_complete_head_bound_operator_review(field):
    pr, item, metadata = owned_update()
    del metadata["1"][field]
    assert not assess([pr], POLICY, metadata, item, update_pr=1)["allowed"]


@pytest.mark.parametrize("field,value", [
    ("head_sha", "a" * 40), ("owner", "other-session"), ("work_id", "other-work"),
    ("head_ref", "other-branch"), ("head_repo", "external/MJobs"),
    ("base_ref", "release"), ("base_repo", "external/MJobs"),
    ("publisher_id", True), ("publisher_id", 7), ("publisher_login", "someone-else"),
])
def test_update_rejects_inconsistent_operator_review(field, value):
    pr, item, metadata = owned_update()
    metadata["1"][field] = value
    assert not assess([pr], POLICY, metadata, item, update_pr=1)["allowed"]


@pytest.mark.parametrize("change", ["closed", "merged", "head_sha", "head_ref", "head_repo",
    "base_ref", "base_repo", "missing_base", "missing_source", "publisher", "bot", "duplicate"])
def test_update_rejects_provider_identity_changes(change):
    pr, item, metadata = owned_update()
    if change == "closed":
        pr["state"] = "closed"
    elif change == "merged":
        pr["merged_at"] = "2026-10-18T20:00:00Z"
    elif change in ("head_sha", "head_ref", "head_repo", "base_ref", "base_repo"):
        section, key = change.split("_")
        pr[section][key] = {"full_name": "external/MJobs"} if key == "repo" else "a" * 40
    elif change == "missing_base":
        del pr["base"]
    elif change == "missing_source":
        pr["head"]["repo"] = None
    elif change == "publisher":
        pr["user"]["id"] = 7
    elif change == "bot":
        pr["user"]["type"] = "Bot"
    prs = [pr, pr] if change == "duplicate" else [pr]
    assert not assess(prs, POLICY, metadata, item, update_pr=1)["allowed"]


def test_update_preserves_other_duplicate_guards_and_over_capacity_block():
    pr, item, metadata = owned_update()
    for other in [pull(2, head=dict(pr["head"])),
                  pull(2, head={"sha": item["head_sha"], "ref": "other"})]:
        result = assess([pr, other], POLICY, metadata, item, update_pr=1)
        assert "duplicate open PR scope/identity: 2" in result["errors"]
    other = pull(2)
    metadata["2"] = dict(metadata["1"], head_sha=other["head"]["sha"])
    assert not assess([pr, other], POLICY, metadata, item, update_pr=1)["allowed"]
    result = assess([pr, pull(2)], {"max_active_prs": 1}, metadata, item, update_pr=1)
    assert "projected development PR capacity exceeded" in result["errors"]
    assert result["projected_development_open"] == 2


def test_update_requires_candidate_and_does_not_trust_pr_authored_identity():
    pr, item, metadata = owned_update()
    pr["body"] = json.dumps(metadata["1"])
    assert not assess([pr], POLICY, {}, item, update_pr=1)["allowed"]
    assert not assess([pr], POLICY, metadata, update_pr=1)["allowed"]


def test_update_repository_case_is_insensitive_but_refs_and_owner_are_exact():
    pr, item, metadata = owned_update()
    item["head_repo"] = item["base_repo"] = "MULATTOTECH/mjobs"
    assert assess([pr], POLICY, metadata, item, update_pr=1)["allowed"]


@pytest.mark.parametrize("section", ["head", "base"])
def test_update_rejects_external_repository_even_when_all_claims_agree(section):
    pr, item, metadata = owned_update()
    pr[section]["repo"]["full_name"] = "external/MJobs"
    item[f"{section}_repo"] = metadata["1"][f"{section}_repo"] = "external/MJobs"
    assert not assess([pr], POLICY, metadata, item, update_pr=1)["allowed"]


@pytest.mark.parametrize("metadata", [None, [], {}, {"1": []}])
def test_update_cannot_use_unreviewed_or_malformed_metadata(metadata):
    pr, item, _ = owned_update()
    assert not assess([pr], POLICY, metadata, item, update_pr=1)["allowed"]


def test_invalid_update_cannot_exclude_target_or_hide_added_capacity():
    pr, item, metadata = owned_update()
    item["owner"] = "unowned"
    result = assess([pr], {"max_active_prs": 1}, metadata, item, update_pr=1)
    assert result["validated_update_pr"] is None
    assert result["projected_development_open"] == 2
    assert "duplicate open PR scope/identity: 1" in result["errors"]
    assert "projected development PR capacity exceeded" in result["errors"]


def test_valid_update_retains_soft_capacity_warning_without_changing_inventory():
    pr, item, metadata = owned_update()
    prs = [pr, pull(2, draft=True)]
    inventory = assess(prs, dict(POLICY, max_active_prs=1), metadata)
    result = assess(prs, dict(POLICY, max_active_prs=1), metadata, item, update_pr=1)
    assert result["allowed"]
    assert result["capacity"] == "WARNING"
    for field in ("development_open", "dependency_open", "drafts", "authorship", "categories", "backlog", "conflicts"):
        assert result[field] == inventory[field]
    assert result["projected_development_open"] == 2


def test_cli_update_uses_live_inventory_and_reviewed_metadata(monkeypatch, capsys):
    pr, item, metadata = owned_update()
    data = {"candidate.json": item, "metadata.json": metadata, "planning/PR_QUEUE.json": {"policy": POLICY}}
    monkeypatch.setattr("sys.argv", ["pr_queue", "--live", "--candidate", "candidate.json",
                                    "--metadata", "metadata.json", "--update-pr", "1"])
    monkeypatch.setattr("pathlib.Path.read_text", lambda path, **kwargs: json.dumps(data[path.as_posix()]))
    calls = []
    monkeypatch.setattr("tools.verification.pr_queue.github_pulls", lambda: calls.append(1) or [pr])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert calls == [1]
    assert result["source"] == "authenticated GitHub REST"
    assert result["validated_update_pr"] == 1


@pytest.mark.parametrize("args", [
    ["--live", "--update-pr", "1"],
    ["--live", "--candidate", "c.json", "--update-pr", "1"],
    ["--snapshot", "s.json", "--candidate", "c.json", "--metadata", "m.json", "--update-pr", "1"],
])
def test_cli_update_requires_live_candidate_and_separate_review(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["pr_queue", *args])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
