"""Regression controls for the final independent CI integrity review."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pytest
import yaml
from _pytest.junitxml import bin_xml_escape, mangle_test_address
from sqlalchemy.engine import make_url

from fixtures.postgres_ci_template import OwnedDatabaseTemplate, disposable_url, require_template_inputs
from tools.ci.junit_identity import canonical_junit_identity, load_hosted_skips
from tools.ci.shard_evidence import verify_shard
from tools.ci.unit_shard_evidence import reconcile as reconcile_unit
from tests.unit.test_shard_evidence import evidence, write_report

ROOT = Path(__file__).resolve().parents[2]
URL = "postgresql+psycopg://mjobs@127.0.0.1:5432/mjobs_test"


@pytest.mark.parametrize("name,value", [
    ("PGHOSTADDR", "192.0.2.1"),
    ("PGHOST", "192.0.2.1"),
    ("PGPORT", "6543"),
    ("PGDATABASE", "unowned"),
    ("PGSERVICE", "unowned"),
    ("PGSERVICEFILE", "unowned-service.conf"),
    ("PGOPTIONS", "-csearch_path=unowned"),
])
def test_inherited_fixture_overrides_rejected_before_engine(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with patch("fixtures.postgres_ci_template.create_engine") as engine:
        with pytest.raises(ValueError, match="Inherited PostgreSQL.*" + name):
            OwnedDatabaseTemplate(URL)
        engine.assert_not_called()
    with pytest.raises(ValueError, match="Inherited PostgreSQL.*" + name):
        require_template_inputs({"MJOBS_TEST_POSTGRES_URL": URL,
                                 "MJOBS_CI_POSTGRES_DUMP_CONTAINER": "a"*64, name: value})


@pytest.mark.parametrize("host,expected", [("localhost","127.0.0.1"),("127.0.0.1","127.0.0.1"),("[::1]","::1")])
def test_fixture_url_pins_numeric_hostaddr_and_port(host, expected):
    url = disposable_url(f"postgresql+psycopg://mjobs@{host}/mjobs_test", env={})
    assert url.host == url.query["hostaddr"] == expected
    assert url.port == 5432
    clone = make_url(url.set(database="mjobs_test_ci_"+"a"*32).update_query_dict(
        {"options":"-csearch_path=fixture"}).render_as_string(hide_password=False))
    assert clone.host == clone.query["hostaddr"] == expected
    assert clone.port == 5432 and clone.query["options"] == "-csearch_path=fixture"


@pytest.mark.parametrize("node", [
    "tests/unit/test_example.py::test_simple",
    "tests/browser/test_example.py::Fixture::test_param[dot.a]",
    "tests/integration/nested/test_example.py::Fixture::test_param[colon::b[inside]]",
    r"tests/unit/test_example.py::test_param[\u03bb]",
])
def test_canonical_identity_matches_actual_pytest(node):
    names = mangle_test_address(node)
    assert canonical_junit_identity(node) == (".".join(names[:-1]), bin_xml_escape(names[-1]))


def test_swapped_manifest_labels_are_rejected(tmp_path):
    target = "tests/browser/test_pair.py"
    m = evidence([target+"::test_one", target+"::test_two"])
    p = tmp_path/"browser.xml"
    write_report(p,m)
    bad = copy.deepcopy(m)
    bad["selected"][0]["name"], bad["selected"][1]["name"] = bad["selected"][1]["name"], bad["selected"][0]["name"]
    with pytest.raises(ValueError, match="node ID does not match JUnit identity"):
        verify_shard(bad,p,[target],split=False)


def test_hosted_skip_allowance_is_exact_and_never_covers_missing_spa(tmp_path):
    allow = load_hosted_skips(ROOT,"browser")
    allowed_node = next(iter(allow))
    required = "tests/browser/test_application_review_answers.py::Fixture::test_answer_resume"
    m = evidence([allowed_node,required])
    targets = sorted({node.split("::",1)[0] for node in (allowed_node,required)})
    p=tmp_path/"browser.xml"
    write_report(p,m,skip=[allowed_node])
    xml = ET.parse(p)
    s=next(xml.iter("skipped"))
    s.set("type","pytest.skip"); s.set("message",allow[allowed_node])
    xml.write(p,encoding="utf-8")
    assert verify_shard(m,p,targets,split=False,allowed_skips=allow)[0]=={allowed_node,required}
    s.set("message","Build apps/web before browser acceptance")
    xml.write(p,encoding="utf-8")
    with pytest.raises(ValueError,match="Unexpected hosted skip"):
        verify_shard(m,p,targets,split=False,allowed_skips=allow)
    write_report(p,m,skip=[required])
    xml=ET.parse(p); s=next(xml.iter("skipped"))
    s.set("type","pytest.skip"); s.set("message",allow[allowed_node])
    xml.write(p,encoding="utf-8")
    with pytest.raises(ValueError,match="Unexpected hosted skip"):
        verify_shard(m,p,targets,split=False,allowed_skips=allow)
    # Native/local evidence still accepts legitimate host-specific skips.
    assert verify_shard(m,p,targets,split=False)[0]=={allowed_node,required}


def test_same_count_unit_substitution_and_unexpected_skip_fail(tmp_path):
    allowed = load_hosted_skips(ROOT,"unit")
    known = next(iter(allowed))
    required = "tests/unit/test_pair.py::test_required[dot.a::b]"
    nodes = sorted([known,required])
    manifest = {"index":0,"count":1,"inventory":nodes,"selected":nodes,"completed":nodes}
    (tmp_path/"unit-collection.json").write_text(json.dumps(manifest),encoding="utf-8")
    p=tmp_path/"unit.xml"
    root=ET.Element("testsuites"); suite=ET.SubElement(root,"testsuite")
    for node in nodes:
        classname,name=canonical_junit_identity(node)
        case=ET.SubElement(suite,"testcase",classname=classname,name=name)
        if node==known:
            ET.SubElement(case,"skipped",type="pytest.skip",message=allowed[node])
        else:
            required_case=case
    ET.ElementTree(root).write(p,encoding="utf-8")
    assert reconcile_unit(tmp_path,1,tmp_path/"combined.xml",single=True,hosted=True)==2
    classname,name=canonical_junit_identity(required)
    required_case.set("name","unrelated_test")
    ET.ElementTree(root).write(p,encoding="utf-8")
    with pytest.raises(ValueError,match="Unexpected unit JUnit identity"):
        reconcile_unit(tmp_path,1,tmp_path/"combined.xml",single=True,hosted=True)
    required_case.set("name",name)
    ET.SubElement(required_case,"skipped",type="pytest.skip",message=allowed[known])
    ET.ElementTree(root).write(p,encoding="utf-8")
    with pytest.raises(ValueError,match="Unexpected hosted skip"):
        reconcile_unit(tmp_path,1,tmp_path/"combined.xml",single=True,hosted=True)


def test_all_hosted_workflows_enforce_skip_contract():
    for suite,count in (("unit",55),("browser",5),("integration",53)):
        assert len(load_hosted_skips(ROOT,suite))==count
    for workflow, commands in (
        ("ci.yml",["tools.ci.unit_shard_evidence"]),
        ("browser-regression.yml",["tools.ci.run_shard","tools.ci.shard_evidence"]),
        ("integration.yml",["tools.ci.run_shard","tools.ci.shard_evidence"]),
    ):
        jobs=yaml.load((ROOT/".github/workflows"/workflow).read_text(encoding="utf-8"),Loader=yaml.BaseLoader)["jobs"]
        for command in commands:
            matching=[step["run"] for job in jobs.values() for step in job.get("steps",[]) if command in step.get("run","")]
            assert len(matching)==1 and "--hosted" in matching[0]
