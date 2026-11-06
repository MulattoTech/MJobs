"""Read-only external-sprint discovery must never grant Mission Control PID ownership."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.agent_dashboard.external_sprints import current_activity, current_round3


def event(kind, payload):
    return json.dumps({"type": kind, "payload": payload}, separators=(",", ":"))


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True)


@pytest.fixture
def activity_fixture(tmp_path):
    repo = tmp_path / "MJobs"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "codex@example.invalid")
    git(repo, "config", "user.name", "Codex Test")
    (repo / "README.md").write_text("fixture\n", encoding="utf8")
    (repo / "1 old.txt").write_text("renamed later\n", encoding="utf8")
    git(repo, "add", "README.md", "1 old.txt")
    git(repo, "commit", "-qm", "fixture")
    linked = tmp_path / "registered-linked-worktree"
    git(repo, "worktree", "add", "-qb", "feature/active", str(linked))
    sessions = tmp_path / "sessions"
    return repo, linked, sessions


def write_rollout(session_root, day, name, events, *, modified=None):
    directory = session_root / day.strftime("%Y/%m/%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf8")
    if modified is not None:
        import os
        os.utime(path, (modified.timestamp(), modified.timestamp()))
    return path


def rollout_event(kind, payload, timestamp=None):
    value = {"timestamp": timestamp, "type": kind, "payload": payload} if timestamp else {
        "type": kind, "payload": payload
    }
    return value


@pytest.fixture
def fixture(tmp_path):
    repo = tmp_path / "MJobs"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    clone = repo / ".mjobs-recovery" / "active" / "MJobs-Round3-20261014"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "switch", "-q", "-c", "codex/p0p1-round3"], check=True)
    state_dir = repo / ".git" / "mjobs-p0p1-round3"
    state_dir.mkdir()
    (state_dir / "latest.json").write_text(json.dumps({
        "worktree": str(clone), "campaign": "round3-20261014",
        "status": "RUNNING", "branch": "codex/p0p1-round3",
    }))
    goal = clone / ".git" / "afk-dev"
    goal.mkdir()
    (goal / "goal.json").write_text(json.dumps({
        "statement": "Finish P0/P1 requirements"
    }))
    (clone / "new-source.py").write_text("print('hello')\n")
    sessions = tmp_path / "sessions"
    day = sessions / "2026" / "10" / "14"
    day.mkdir(parents=True)
    (day / "rollout-2026-10-14T15-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl").write_text(
        "\n".join([
            event("session_meta", {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                                   "cwd": str(clone), "source": "cli"}),
            event("turn_context", {"model": "gpt-6.1-sol", "effort": "high"}),
            event("response_item", {"type": "function_call", "name": "spawn_agent"}),
            event("event_msg", {"type": "token_count", "info": {
                "total_token_usage": {
                    "input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 40}}}),
        ]) + "\n", encoding="utf8")
    (day / "rollout-2026-10-14T15-01-00-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl").write_text(
        "\n".join([
            event("session_meta", {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "cwd": str(clone),
                "source": {"subagent": {"thread_spawn": {
                    "parent_thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "agent_nickname": "Tesla"}}}}),
            event("turn_context", {"model": "gpt-6-luna", "effort": "high"}),
            event("event_msg", {"type": "token_count", "info": {
                "total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}}}),
        ]) + "\n", encoding="utf8")
    return repo, clone, sessions, day


def test_active_external_codex_discovered_without_ownership(fixture):
    repo, clone, sessions, _ = fixture
    process = {"Name": "codex.exe", "ProcessId": 5522,
               "CommandLine": f"codex.exe -C {clone} -m gpt-6.1-sol"}
    result = current_round3(repo, session_root=sessions, processes=[process])
    assert result["available"] and result["read_only"]
    assert result["observation"] == "RUNNING"
    assert result["pids"] == [5522]
    assert result["branch"] == "codex/p0p1-round3"
    assert result["changed_count"] == 1
    assert result["subagent_count"] == 1
    assert {x["name"] for x in result["sessions"]} == {"Root agent", "Tesla"}
    assert result["root_session"]["model"] == "gpt-6.1-sol"
    assert result["tokens"] == {"input_tokens": 400, "cached_tokens": 280,
                                "output_tokens": 60}
    assert result["goal"] == "Finish P0/P1 requirements"
    assert "stop" in result["note"].lower()  # Observation must never imply ownership.
    assert not (clone / "new-source.py").read_text() == ""


def test_stopped_process_is_not_misrepresented_as_running(fixture):
    repo, _, sessions, _ = fixture
    result = current_round3(repo, session_root=sessions, processes=[])
    assert result["checkpoint_status"] == "RUNNING"
    assert result["observation"] == "INACTIVE"
    assert result["pids"] == []


def test_wrong_workspace_session_is_excluded(fixture):
    repo, _, sessions, day = fixture
    (day / "rollout-2026-10-14T15-02-00-cccccccc-cccc-cccc-cccc-cccccccccccc.jsonl").write_text(
        event("session_meta", {"cwd": str(repo / "WrongWorkspace"),
                               "id": "cccccccc-cccc-cccc-cccc-cccccccccccc"}) + "\n",
        encoding="utf8",
    )
    result = current_round3(repo, session_root=sessions, processes=[])
    assert len(result["sessions"]) == 2


def test_outside_checkout_is_rejected_without_access(tmp_path):
    repo = tmp_path / "MJobs"
    state_dir = repo / ".git" / "mjobs-p0p1-round3"
    state_dir.mkdir(parents=True)
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    (outside / ".git").mkdir()
    (state_dir / "latest.json").write_text(json.dumps({"worktree": str(outside)}))
    result = current_round3(repo, session_root=tmp_path, processes=[])
    assert result["available"] is False
    assert result["reason"] == "Campaign is outside the canonical archive"


def test_missing_campaign_gives_clear_not_available(tmp_path):
    assert current_round3(tmp_path, processes=[])["available"] is False


def test_observer_does_not_change_checkout_or_checkpoint(fixture):
    repo, clone, sessions, _ = fixture
    files = {p: p.read_bytes() for p in (
        repo / ".git" / "mjobs-p0p1-round3" / "latest.json",
        clone / "new-source.py", clone / ".git" / "afk-dev" / "goal.json"
    )}
    current_round3(repo, session_root=sessions, processes=[])
    assert all(p.read_bytes() == original for p, original in files.items())


def test_recent_linked_worktree_activity_supersedes_historical_checkpoint(activity_fixture):
    repo, linked, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    write_rollout(sessions, now.date(), "rollout-current.jsonl", [
        rollout_event("session_meta", {"id": "new", "cwd": str(repo), "source": "cli"}),
        rollout_event("turn_context", {"cwd": str(linked), "model": "gpt-6-luna",
                                        "effort": "high"}, "2026-11-06T17:59:00Z"),
        rollout_event("response_item", {"type": "message", "content": "private message body"}),
        rollout_event("event_msg", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 120, "output_tokens": 9}}}),
    ])
    result = current_activity(repo, session_root=sessions, processes=[], now=now)
    assert result["available"] and result["read_only"]
    assert result["observation"] == "RECENT_ACTIVITY"
    assert {item["workspace"] for item in result["workspaces"]} == {str(repo), str(linked)}
    assert result["latest_workspace"] == str(linked)
    assert result["sessions"][0]["workspace"] == str(linked)
    assert next(item for item in result["workspaces"] if item["workspace"] == str(linked))["branch"] == "feature/active"
    assert result["sessions"][0]["input_tokens"] == 120
    assert result["sessions"][0]["cached_tokens"] is None
    assert result["sessions"][0]["output_tokens"] == 9
    assert "private message body" not in json.dumps(result)


def test_recent_log_activity_does_not_claim_running_without_process_evidence(activity_fixture):
    repo, linked, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    write_rollout(sessions, now.date(), "rollout-current.jsonl", [
        rollout_event("session_meta", {"id": "new", "cwd": str(linked)}),
        rollout_event("turn_context", {"cwd": str(linked)}, "2026-11-06T17:59:00Z"),
    ])
    result = current_activity(repo, session_root=sessions, processes=[], now=now)
    assert result["observation"] == "RECENT_ACTIVITY"


def test_stale_logs_are_not_selected_as_recent_activity(activity_fixture):
    repo, _, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    stale_day = now.date() - timedelta(days=2)
    write_rollout(sessions, stale_day, "rollout-stale.jsonl", [
        rollout_event("session_meta", {"id": "old", "cwd": str(repo)}),
        rollout_event("turn_context", {"cwd": str(repo)}, "2026-11-04T18:00:00Z"),
    ], modified=now - timedelta(days=2))
    result = current_activity(repo, session_root=sessions, processes=[], now=now)
    assert result["observation"] == "NO_RECENT_ACTIVITY"
    assert result["sessions"] == []
    assert result["latest_workspace"] is None


def test_large_rollout_uses_header_and_recent_metadata_without_exposing_output(activity_fixture):
    repo, linked, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    directory = sessions / now.strftime("%Y/%m/%d")
    directory.mkdir(parents=True)
    path = directory / "rollout-large.jsonl"
    path.write_text("\n".join([
        json.dumps(rollout_event("session_meta", {
            "id": "large", "cwd": str(repo), "metadata_padding": "m" * 20000,
        })),
        json.dumps(rollout_event("response_item", {"type": "message", "content": "x" * 180000})),
        json.dumps(rollout_event("turn_context", {"cwd": str(linked), "model": "gpt-6-luna"},
                                 "2026-11-06T17:59:00Z")),
        json.dumps(rollout_event("response_item", {"type": "message", "content": "secret" * 20000})),
    ]) + "\n", encoding="utf8")
    import os
    os.utime(path, (now.timestamp(), now.timestamp()))
    result = current_activity(repo, session_root=sessions, processes=[], now=now)
    assert result["sessions"][0]["workspace"] == str(linked)
    assert "secret" not in json.dumps(result)


def test_recent_session_file_limit_marks_coverage_partial(activity_fixture):
    repo, _, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    directory = sessions / now.strftime("%Y/%m/%d")
    directory.mkdir(parents=True)
    import os
    for index in range(161):
        path = directory / f"rollout-{index:03}.jsonl"
        path.write_text("{}\n", encoding="utf8")
        os.utime(path, (now.timestamp(), now.timestamp()))
    result = current_activity(repo, session_root=sessions, processes=[], now=now)
    assert result["coverage"]["partial"] is True
    assert result["coverage"]["truncated"] is True


def test_unrelated_recent_repository_session_is_excluded(activity_fixture, tmp_path):
    repo, _, sessions = activity_fixture
    unrelated = tmp_path / "OtherRepo"
    unrelated.mkdir()
    git(unrelated, "init", "-q")
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    write_rollout(sessions, now.date(), "rollout-other.jsonl", [
        rollout_event("session_meta", {"id": "other", "cwd": str(unrelated)}),
        rollout_event("turn_context", {"cwd": str(unrelated)}, "2026-11-06T17:59:00Z"),
    ])
    result = current_activity(repo, session_root=sessions, processes=[], now=now)
    assert result["observation"] == "NO_RECENT_ACTIVITY"
    assert result["sessions"] == []
    assert {item["workspace"] for item in result["workspaces"]} == {str(repo)}


def test_status_count_is_not_limited_by_changed_files_preview(activity_fixture):
    repo, linked, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    write_rollout(sessions, now.date(), "rollout-current.jsonl", [
        rollout_event("session_meta", {"id": "active", "cwd": str(linked)}),
        rollout_event("turn_context", {"cwd": str(linked)}, "2026-11-06T17:59:00Z"),
    ])
    for index in range(105):
        (linked / f"changed-{index:03}.txt").write_text("changed\n", encoding="utf8")
    result = current_activity(repo, session_root=sessions, processes=[], now=now)
    linked_result = next(item for item in result["workspaces"] if item["workspace"] == str(linked))
    assert linked_result["changed_count"] == 105
    assert len(linked_result["changed_files"]) == 30
    assert result["observation"] == "RECENT_ACTIVITY"


def test_porcelain_v2_rename_origin_is_not_counted_as_a_second_change(activity_fixture):
    repo, linked, _ = activity_fixture
    (linked / "1 old.txt").rename(linked / "2 new.txt")
    git(linked, "add", "-A")
    from tools.agent_dashboard.external_sprints import _workspace_observation
    result = _workspace_observation(linked, [])
    assert result["changed_count"] == 1
    assert result["changed_files"] == ["2 new.txt"]


def test_porcelain_v2_unmerged_record_path_parses_after_all_fields(activity_fixture, monkeypatch):
    repo, _, _ = activity_fixture
    import tools.agent_dashboard.external_sprints as observer
    original_git = observer._git
    unmerged = ("# branch.oid deadbeef\n# branch.head conflict\n"
                "u UU N... 100644 100644 100644 100644 aaaaaaaa bbbbbbbb cccccccc conflict.txt\0")

    def conflict_status(cwd, *args, **kwargs):
        if "status" in args:
            return unmerged
        return original_git(cwd, *args, **kwargs)

    monkeypatch.setattr(observer, "_git", conflict_status)
    result = observer._workspace_observation(repo, [])
    assert result["changed_count"] == 1
    assert result["changed_files"] == ["conflict.txt"]


def test_git_status_failure_makes_changed_count_unknown(activity_fixture, monkeypatch):
    repo, linked, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    write_rollout(sessions, now.date(), "rollout-current.jsonl", [
        rollout_event("session_meta", {"id": "active", "cwd": str(linked)}),
    ])
    import tools.agent_dashboard.external_sprints as observer
    original_git = observer._git

    def failing_status(cwd, *args, **kwargs):
        if "status" in args:
            return None
        return original_git(cwd, *args, **kwargs)

    monkeypatch.setattr(observer, "_git", failing_status)
    result = current_activity(repo, session_root=sessions, processes=[],
                              now=now)
    assert next(item for item in result["workspaces"] if item["workspace"] == str(linked))[
        "changed_count"] is None


def test_running_requires_matching_verified_codex_process(activity_fixture):
    repo, linked, _ = activity_fixture
    process = {"Name": "codex.exe", "ProcessId": 98,
               "CommandLine": f'codex.exe --cwd "\\\\?\\{linked}"'}
    result = current_activity(repo, session_root=repo / "no-sessions", processes=[process],
                              now=datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc))
    assert result["observation"] == "RUNNING"
    assert result["pids"] == [98]


def test_process_path_text_without_explicit_exact_cwd_is_not_running(activity_fixture):
    repo, linked, _ = activity_fixture
    process = {"Name": "codex.exe", "ProcessId": 99,
               "CommandLine": f'codex.exe --profile {linked}-neighbor'}
    result = current_activity(repo, session_root=repo / "no-sessions", processes=[process],
                              now=datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc))
    assert result["observation"] == "NO_RECENT_ACTIVITY"
    assert result["pids"] == []


def test_observer_does_not_write_to_repo_or_session_logs(activity_fixture):
    repo, linked, sessions = activity_fixture
    now = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    rollout = write_rollout(sessions, now.date(), "rollout-readonly.jsonl", [
        rollout_event("session_meta", {"id": "new", "cwd": str(linked)}),
    ])
    before = {str(path): path.read_bytes() for path in [rollout, repo / "README.md"]}
    current_activity(repo, session_root=sessions, processes=[], now=now)
    assert {path: Path(path).read_bytes() for path in before} == before

def test_mission_control_endpoint_reports_only_readonly_observer(fixture, monkeypatch):
    from fastapi.testclient import TestClient
    from tools.agent_dashboard import server as dashboard_server

    repo, _, _, _ = fixture
    readonly_observer = lambda _root, processes: {
            "available": True,
            "read_only": True,
            "observation": "RUNNING",
            "sessions": [{"name": "Tesla", "model": "gpt-6-luna"}],
        }
    monkeypatch.setattr(dashboard_server, "current_activity", readonly_observer, raising=False)
    monkeypatch.setattr(dashboard_server, "current_round3", readonly_observer)
    monkeypatch.setattr(dashboard_server, "windows_process_snapshot", lambda: [])
    quota_preferences = []

    def quota_reader(*, prefer_fleet: bool):
        quota_preferences.append(prefer_fleet)
        assert prefer_fleet is True
        return {"available": True, "used_percent": 73, "remaining_percent": 27}

    monkeypatch.setattr(
        dashboard_server, "read_codex_rate_limits", quota_reader,
    )
    app = dashboard_server.create_app(repo, mission_instance=True)
    with TestClient(app) as client:
        reply = client.get("/api/sprints/active")
        assert reply.status_code == 200
        assert reply.json()["sessions"][0]["name"] == "Tesla"
        assert reply.json()["read_only"] is True
        assert reply.json()["quota"]["used_percent"] == 73
        assert quota_preferences == [True]
        # Read-only discovery must not create stop/restart controls for external agents.
        assert client.get("/api/launcher/health").status_code == 200
        token = app.state.mission_control.token
        payload = {"profile": "codex-sol-6-1-high", "model": "gpt-6.1-sol",
                   "effort": "high", "max_subagents": 4,
                   "intended_subagents": 2}
        blocked = client.post(
            "/api/launcher/codex/launch",
            headers={"X-MJobs-Launch-Token": token}, json=payload,
        )
        assert blocked.status_code == 409
        assert "already running" in blocked.text
