"""Offline regressions: background probes must not allocate Windows consoles."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    ("supervisor.py", "_run"),
    ("model_catalog.py", "_run"),
    ("external_sprints.py", "_git"),
    ("mission_control.py", "git"),
)


def function_from_source(filename, name, **namespace):
    source = ROOT / "tools" / "agent_dashboard" / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found = [node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(found) == 1, (filename, name)
    node = copy.deepcopy(found[0])
    node.decorator_list = []
    isolated = ast.Module(body=[
        ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations")], level=0),
        node,
    ], type_ignores=[])
    ast.fix_missing_locations(isolated)
    scope = {"Path": Path, "json": json, **namespace}
    exec(compile(isolated, str(source), "exec"), scope)
    return scope[name]


def call_helper(filename, function):
    cwd = Path("C:/isolated-test")
    if filename in ("supervisor.py", "model_catalog.py"):
        return function(["git", "status"], cwd, timeout=7)
    return function(cwd, "status")


@pytest.mark.parametrize("filename,name", CASES)
@pytest.mark.parametrize("windows", [True, False])
def test_background_helpers_use_no_console_flags(filename, name, windows):
    result = subprocess.CompletedProcess(["fixture"], 0, "fixture-output\n", "")
    runner = Mock(return_value=result)
    fake = SimpleNamespace(run=runner, TimeoutExpired=subprocess.TimeoutExpired)
    if windows:
        fake.CREATE_NO_WINDOW = 0x08000000
    function = function_from_source(filename, name, subprocess=fake)
    returned = call_helper(filename, function)
    runner.assert_called_once()
    kwargs = runner.call_args.kwargs
    assert kwargs["creationflags"] == (0x08000000 if windows else 0)
    assert kwargs.get("shell", False) is False
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] > 0
    if filename in ("supervisor.py", "mission_control.py"):
        assert returned is result
    elif filename == "model_catalog.py":
        assert returned == "fixture-output\n"
    else:
        assert returned == "fixture-output"


@pytest.mark.parametrize("filename,name", CASES)
def test_nonzero_exit_contract_is_unchanged(filename, name):
    result = subprocess.CompletedProcess(["fixture"], 1, "", "fixture-error")
    fake = SimpleNamespace(run=Mock(return_value=result),
                           TimeoutExpired=subprocess.TimeoutExpired)
    function = function_from_source(filename, name, subprocess=fake)
    if filename == "model_catalog.py":
        with pytest.raises(RuntimeError):
            call_helper(filename, function)
    elif filename == "external_sprints.py":
        assert call_helper(filename, function) is None
    else:
        assert call_helper(filename, function) is result


@pytest.mark.parametrize("filename,name", CASES)
@pytest.mark.parametrize("failure", [OSError("fixture"),
    subprocess.TimeoutExpired(["fixture"], 7)])
def test_existing_command_failure_contracts_are_preserved(filename, name, failure):
    fake = SimpleNamespace(run=Mock(side_effect=failure),
                           TimeoutExpired=subprocess.TimeoutExpired)
    function = function_from_source(filename, name, subprocess=fake)
    if filename == "external_sprints.py":
        assert call_helper(filename, function) is None
    else:
        with pytest.raises(type(failure)):
            call_helper(filename, function)


@pytest.mark.parametrize("failure", [
    OSError("sensitive fixture path"),
    subprocess.TimeoutExpired(["sensitive fixture command"], 6),
])
def test_gpu_probe_failures_are_unavailable_not_http_failures(failure):
    probe = Mock(side_effect=failure)
    function = function_from_source("observability.py", "windows_gpu_snapshot",
        os=SimpleNamespace(name="nt"), subprocess=subprocess, _run=probe)
    result = function()
    assert result == {"available": False,
                      "error": "GPU telemetry temporarily unavailable"}
    assert "sensitive" not in json.dumps(result)
    probe.assert_called_once()
    assert probe.call_args.kwargs["timeout"] == 6


def test_gpu_success_payload_is_preserved():
    payload = [{"pid": 42, "value": 36.5}, {"pid": 42, "value": 10}]
    probe = Mock(return_value=subprocess.CompletedProcess(
        ["fixture"], 0, json.dumps(payload), ""))
    function = function_from_source("observability.py", "windows_gpu_snapshot",
        os=SimpleNamespace(name="nt"), subprocess=subprocess, _run=probe)
    assert function() == {"available": True, "utilization_percent": 36.5,
                           "by_pid": {"42": 36.5}}


def test_gpu_non_windows_remains_unavailable_without_a_probe():
    probe = Mock()
    function = function_from_source("observability.py", "windows_gpu_snapshot",
        os=SimpleNamespace(name="posix"), subprocess=subprocess, _run=probe)
    assert function() == {"available": False}
    probe.assert_not_called()


def test_unrelated_gpu_programming_errors_are_not_suppressed():
    probe = Mock(side_effect=ValueError("fixture"))
    function = function_from_source("observability.py", "windows_gpu_snapshot",
        os=SimpleNamespace(name="nt"), subprocess=subprocess, _run=probe)
    with pytest.raises(ValueError):
        function()
