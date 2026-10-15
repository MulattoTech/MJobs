"""Deterministic publication-policy regressions; no GitHub writes or credentials."""
import json
from types import SimpleNamespace

import pytest

from tools.verification.pr_queue import assess, classify, github_pulls, main, validate_policy


POLICY = {"max_active_prs": 32, "capacity_behavior": "warn"}


def pull(number=1, **updates):
    result = {"number": number, "state": "open", "draft": False,
              "user": {"id": 5900632, "login": "MulattoTech", "type": "User"},
              "head": {"sha": f"{number:040x}", "ref": f"feature/{number}"},
              "title": "Bump dependencies", "mergeable": None}
    result.update(updates)
    return result


def candidate():
    return {"head_sha": "f" * 40, "head_ref": "feature/new", "work_id": "policy-32",
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
