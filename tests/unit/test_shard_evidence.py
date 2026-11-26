"""Negative controls for exact execution evidence and opt-in CI proof."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml

from tools.ci.integration_split import RECOVERY_FILE, SPLIT_NODES
from tools.ci.run_shard import select_files
from tools.ci.shard_evidence import PROOF_NAME, TEMPLATE_TARGET, reconcile, verify_shard, verify_template_proof
from fixtures.postgres_ci_template import require_template_inputs

ROOT = Path(__file__).resolve().parents[2]


def evidence(nodes, excluded=()):
    included = sorted(set(nodes) - set(excluded))
    selected = []
    for node in included:
        path, *names = node.split("::")
        selected.append({"nodeid": node, "classname": ".".join([path.removesuffix(".py").replace("/", "."), *names[:-1]]),
                         "name": names[-1]})
    return {"inventory": list(nodes), "excluded": list(excluded),
            "selected": selected, "completed": included}


def write_report(path, manifest, *, skip=()):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for row in manifest["selected"]:
        case = ET.SubElement(suite, "testcase", classname=row["classname"], name=row["name"], time="0.1")
        if row["nodeid"] in skip:
            ET.SubElement(case, "skipped")
    ET.ElementTree(root).write(path, encoding="utf-8")


def test_missing_case_inside_reported_file_is_rejected(tmp_path):
    target = "tests/browser/test_pair.py"
    m = evidence([target+"::test_a", target+"::test_b"])
    p = tmp_path/"browser-0.xml"
    write_report(p, m)
    assert len(verify_shard(m, p, [target], split=False)[0]) == 2
    xml = ET.parse(p)
    suite = xml.getroot().find("testsuite")
    suite.remove(suite.findall("testcase")[1])
    xml.write(p, encoding="utf-8")
    with pytest.raises(ValueError, match="Missing exact JUnit identities"):
        verify_shard(m, p, [target], split=False)


@pytest.mark.parametrize("field", ["inventory", "completed", "selected"])
def test_duplicate_or_missing_manifest_evidence_is_rejected(tmp_path, field):
    target = "tests/integration/test_pair.py"
    m = evidence([target+"::test_a", target+"::test_b"])
    p = tmp_path/"integration-0.xml"
    write_report(p, m)
    m[field].append(copy.deepcopy(m[field][0]))
    with pytest.raises(ValueError):
        verify_shard(m, p, [target], split=False)


def test_recovery_split_requires_only_its_two_declared_exclusions(tmp_path):
    nodes = [RECOVERY_FILE+"::RqWorkhorseRecoveryTests::test_other", *SPLIT_NODES]
    p = tmp_path/"integration-0.xml"
    m = evidence(nodes, SPLIT_NODES)
    write_report(p, m)
    assert verify_shard(m, p, [RECOVERY_FILE], split=True)[0] == {nodes[0]}
    for node in SPLIT_NODES:
        exact = evidence([node])
        write_report(p, exact)
        assert verify_shard(exact, p, [node], split=True)[0] == {node}
    invalid = evidence(nodes, [nodes[0], *SPLIT_NODES[1:]])
    write_report(p, invalid)
    with pytest.raises(ValueError, match="collection or completion"):
        verify_shard(invalid, p, [RECOVERY_FILE], split=True)


def test_full_matrix_fails_on_missing_shard_and_mismatched_metadata(tmp_path):
    root = tmp_path/"repo"
    folder = root/"tests/browser"
    folder.mkdir(parents=True)
    for name in ("test_a.py", "test_b.py"):
        (folder/name).touch()
    artifacts = tmp_path/"matrix"
    artifacts.mkdir()
    for index in range(2):
        targets = select_files(root, "browser", index, 2)
        m = evidence([target+"::test_case" for target in targets])
        m.update(suite="browser", index=index, count=2, targets=targets)
        dest = artifacts/f"browser-results-{index}"
        dest.mkdir()
        write_report(dest/f"browser-{index}.xml", m)
        (dest/f"browser-{index}-collection.json").write_text(json.dumps(m), encoding="utf-8")
    assert reconcile(artifacts, "browser", 2, root=root) == 2
    p = artifacts/"browser-results-1/browser-1-collection.json"
    m = json.loads(p.read_text())
    m["index"] = 0
    p.write_text(json.dumps(m))
    with pytest.raises(ValueError, match="metadata"):
        reconcile(artifacts, "browser", 2, root=root)
    (artifacts/"browser-results-1").rename(tmp_path/"removed")
    with pytest.raises(ValueError, match="artifact directory"):
        reconcile(artifacts, "browser", 2, root=root)


def test_template_proof_missing_invalid_or_skipped_fails(tmp_path):
    node = TEMPLATE_TARGET+"::Fixture::test_proof"
    m = evidence([node])
    p = tmp_path/"integration-0.xml"
    proof = tmp_path/PROOF_NAME
    write_report(p, m)
    with pytest.raises(ValueError, match="proof is missing"):
        verify_shard(m, p, [TEMPLATE_TARGET], split=False, postgres_template=True)
    data = {"full_dump_sha256": "a"*64, "schema_data_owner_acl_equivalence": True,
            "cross_clone_and_future_clone_isolation": True, "fresh_migration_seconds": 1,
            "clone_seconds": [0.2, 0.3], "template_initialization_seconds": 1,
            "total_proof_setup_seconds": 2}
    proof.write_text(json.dumps(data), encoding="utf-8")
    assert verify_shard(m, p, [TEMPLATE_TARGET], split=False, postgres_template=True)[0] == {node}
    write_report(p, m, skip=[node])
    with pytest.raises(ValueError, match="fixture was skipped"):
        verify_shard(m, p, [TEMPLATE_TARGET], split=False, postgres_template=True)
    data["cross_clone_and_future_clone_isolation"] = False
    proof.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not proved"):
        verify_template_proof(proof)


@pytest.mark.parametrize("env,pattern", [
    ({}, "MJOBS_TEST_POSTGRES_URL"),
    ({"MJOBS_TEST_POSTGRES_URL": "postgresql+psycopg://mjobs@127.0.0.1/mjobs_test"}, "dump container ID"),
    ({"MJOBS_TEST_POSTGRES_URL": "postgresql+psycopg://mjobs@invalid.example/mjobs_test",
      "MJOBS_CI_POSTGRES_DUMP_CONTAINER": "a"*64}, "loopback disposable"),
])
def test_opt_in_requires_safe_inputs(env, pattern):
    with pytest.raises(ValueError, match=pattern):
        require_template_inputs(env)


def test_actual_pytest_manifest_matches_parametrized_and_skipped_junit(tmp_path):
    folder = tmp_path/"tests/browser"
    folder.mkdir(parents=True)
    (folder/"test_fixture.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1,2], ids=['dot.a','colon::b'])\n"
        "def test_param(value): assert value\n"
        "@pytest.mark.skip(reason='expected synthetic skip')\n"
        "def test_skip(): pass\n", encoding="utf-8")
    report = tmp_path/"browser-0.xml"
    manifest = tmp_path/"collection.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for name in ("PYTEST_PLUGINS", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    env["MJOBS_CI_SHARD_COLLECTION_PATH"] = str(manifest)
    env["MJOBS_CI_EXCLUDE_NODES"] = "[]"
    args = [sys.executable, "-m", "pytest", "-q", "-p", "tools.ci.shard_collection",
            "--rootdir", str(tmp_path), "tests/browser/test_fixture.py", "--junitxml", str(report)]
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout+result.stderr
    m = json.loads(manifest.read_text())
    assert len(verify_shard(m, report, ["tests/browser/test_fixture.py"], split=False)[0]) == 3
    omitted = subprocess.run([*args, "-k", "test_skip"], cwd=tmp_path, env=env,
                             capture_output=True, text=True, timeout=60)
    assert omitted.returncode != 0
    assert not manifest.exists()


def test_ci_matrix_preserves_documented_cap_and_required_gate():
    doc = yaml.load((ROOT/".github/workflows/integration.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = doc["jobs"]
    strategy = jobs["shard"]["strategy"]
    assert strategy["max-parallel"] == "3"
    expr = strategy["matrix"]["shard"]
    assert expr.endswith("|| '[0,1,2]') }}")
    for size in (3, 4, 6, 8, 10, 12):
        assert f"inputs.shards == '{size}' && '{json.dumps(list(range(size)), separators=(',', ':'))}'" in expr
    assert jobs["required"]["name"] == "Integration / PostgreSQL and Redis"
    assert jobs["required"]["needs"] == "shard"
    assert "tools.ci.shard_evidence" in jobs["required"]["steps"][-1]["run"]
    browser = yaml.load((ROOT/".github/workflows/browser-regression.yml").read_text(), Loader=yaml.BaseLoader)
    assert browser["jobs"]["reconcile"]["needs"] == "browser"
