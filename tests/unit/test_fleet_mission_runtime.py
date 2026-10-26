"""The installed dashboard uses verified code without changing active main."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "tools/fleet_control/Run-Mission-Control.py"
spec = importlib.util.spec_from_file_location("fleet_mission_entry", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture_code(root):
    source = root / "tools/agent_dashboard/server.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture, no process launched\n", encoding="utf-8")
    data = {"schemaVersion": 1, "files": {"tools/agent_dashboard/server.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
    (root / "source-manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return source, data


def test_verified_dashboard_snapshot(tmp_path):
    fixture_code(tmp_path)
    assert module.verified_runtime(tmp_path) == tmp_path.resolve()


def test_changed_code_is_not_silently_launched(tmp_path):
    source, _ = fixture_code(tmp_path)
    source.write_text("# changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="differs"):
        module.verified_runtime(tmp_path)


def test_manifest_cannot_read_outside_runtime(tmp_path):
    _, data = fixture_code(tmp_path)
    data["files"]["../outside.py"] = "0" * 64
    (tmp_path / "source-manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RuntimeError, match="path is invalid"):
        module.verified_runtime(tmp_path)


def test_missing_server_is_not_a_complete_snapshot(tmp_path):
    _, data = fixture_code(tmp_path)
    data["files"] = {"source-manifest.json": "0" * 64}
    (tmp_path / "source-manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RuntimeError):
        module.verified_runtime(tmp_path)


def test_namespace_package_path_is_supported_without_insert(tmp_path):
    from types import SimpleNamespace

    class NamespacePath:
        def __iter__(self):
            return iter(["original/tools"])

    package = SimpleNamespace(__path__=NamespacePath())
    module.prepend_runtime_path(package, tmp_path)
    assert package.__path__ == [str(tmp_path / "tools"), "original/tools"]


def test_original_namespace_repository_and_snapshot_import_in_fresh_process(tmp_path):
    import subprocess
    import sys

    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    code = tmp_path / "verified"
    source, _ = fixture_code(code)
    source.write_text("VALUE = 'reviewed-snapshot'\n", encoding="utf-8")
    script = (
        "import importlib,runpy,sys;from pathlib import Path;"
        "m=runpy.run_path(sys.argv[1],run_name='fixture');"
        "sys.path.insert(0,sys.argv[2]);p=importlib.import_module('tools');"
        "m['prepend_runtime_path'](p,Path(sys.argv[3]));"
        "s=importlib.import_module('tools.agent_dashboard.server');"
        "assert s.VALUE=='reviewed-snapshot';print('PASS namespace overlay')"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script, str(SOURCE), str(repo), str(code)],
                            capture_output=True, text=True, timeout=10, check=True)
    assert "PASS namespace overlay" in result.stdout
