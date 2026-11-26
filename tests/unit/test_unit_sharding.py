"""Verify isolated unit scheduling, actual pytest collection, and fail-closed reconciliation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tools.ci.unit_shard_evidence import reconcile, verify_manifests
from tools.ci.unit_shard_selection import split_inventory


def test_weighted_unit_assignment_is_disjoint_and_keeps_new_cases():
    nodes = ['tests/unit/test_a.py::test_one', 'tests/unit/test_a.py::test_two',
             'tests/unit/test_b.py::test_b', 'tests/unit/test_new.py::test_new']
    groups = split_inventory(nodes, 2, {'tests/unit/test_a.py': 100})
    assert sorted(node for group in groups for node in group) == sorted(nodes)
    assert any(set(nodes[:2]) <= set(group) for group in groups)
    assert groups == split_inventory(list(reversed(nodes)), 2, {'tests/unit/test_a.py': 100})


def test_manifest_negative_controls():
    inventory = ['a', 'b']
    first = {'index': 0, 'count': 2, 'inventory': inventory, 'selected': ['a'], 'completed': ['a']}
    second = {'index': 1, 'count': 2, 'inventory': inventory, 'selected': ['b'], 'completed': ['b']}
    assert verify_manifests([first, second], 2) == 2
    for altered in ({**second, 'selected': ['a'], 'completed': ['a']},
                    {**second, 'completed': []}, {**second, 'index': 0},
                    {**second, 'inventory': ['b']}, {**second, 'count': 3}):
        with pytest.raises(ValueError):
            verify_manifests([first, altered], 2)
    with pytest.raises(ValueError):
        verify_manifests([first], 2)


def test_real_pytest_shards_and_combined_report(tmp_path):
    root = Path(__file__).resolve().parents[2]
    suite = tmp_path / 'tests/unit'
    suite.mkdir(parents=True)
    (tmp_path / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    (suite / 'test_a.py').write_text(
        'import os\n'
        'def test_a():\n'
        '    assert "MJOBS_CI_UNIT_SHARD_INDEX" not in os.environ\n'
        '    assert "tools.ci.unit_shard_selection" not in os.environ.get("PYTEST_PLUGINS", "")\n',
        encoding='utf-8')
    (suite / 'test_b.py').write_text(
        'import pytest\n@pytest.mark.skip(reason="existing skip")\ndef test_b(): pass\n', encoding='utf-8')
    reports = tmp_path / 'reports'
    output = tmp_path / 'outputs/ci'
    for index in range(2):
        env = os.environ.copy()
        env.update(PYTHONPATH=str(root), PYTEST_PLUGINS='tools.ci.unit_shard_selection',
                   MJOBS_CI_UNIT_SHARD_INDEX=str(index), MJOBS_CI_UNIT_SHARD_COUNT='2')
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests/unit',
                                 '--junitxml=outputs/ci/unit.xml'],
                                cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        dest = reports / f'unit-shard-{index}'
        dest.mkdir(parents=True)
        for name in ('unit.xml', 'unit-collection.json'):
            (output / name).replace(dest / name)
    assert reconcile(reports, 2, output / 'combined.xml') == 2
    assert 'skipped="1"' in (output / 'combined.xml').read_text(encoding='utf-8')
    # A same-sized report with a duplicate identity must not qualify.
    first_xml = reports / 'unit-shard-0/unit.xml'
    second_xml = reports / 'unit-shard-1/unit.xml'
    original = second_xml.read_bytes()
    first_case = next(ET.parse(first_xml).iter('testcase'))
    second_tree = ET.parse(second_xml)
    second_case = next(second_tree.iter('testcase'))
    for attr in ('classname', 'name'):
        second_case.set(attr, first_case.attrib[attr])
    second_tree.write(second_xml, encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate or missing unit JUnit identity'):
        reconcile(reports, 2, output / 'combined.xml')
    second_xml.write_bytes(original)
    bad = reports / 'unit-shard-0/unit-collection.json'
    data = json.loads(bad.read_text(encoding='utf-8'))
    data['completed'] = []
    bad.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        reconcile(reports, 2, output / 'combined.xml')

def test_single_runner_reconciles_full_inventory(tmp_path):
    nodes = ['tests/unit/test_pair.py::test_a', 'tests/unit/test_pair.py::test_b']
    manifest = {'index': 0, 'count': 1, 'inventory': nodes,
                'selected': nodes, 'completed': nodes}
    (tmp_path / 'unit-collection.json').write_text(json.dumps(manifest), encoding='utf-8')
    report = tmp_path / 'unit.xml'
    report.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="1">'
                      '<testcase classname="tests.unit.test_pair" name="test_a"/>'
                      '<testcase classname="tests.unit.test_pair" name="test_b"><skipped/></testcase>'
                      '</testsuite></testsuites>', encoding='utf-8')
    assert reconcile(tmp_path, 1, report, single=True) == 2
    assert len(list(ET.parse(report).iter('testcase'))) == 2
    with pytest.raises(ValueError, match='exactly one shard'):
        reconcile(tmp_path, 2, report, single=True)
    manifest['completed'] = ['a']
    (tmp_path / 'unit-collection.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        reconcile(tmp_path, 1, report, single=True)

def test_actual_single_runner_collection_and_evidence(tmp_path):
    root = Path(__file__).resolve().parents[2]
    suite = tmp_path / 'tests/unit'
    suite.mkdir(parents=True)
    (tmp_path / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    (suite / 'test_one.py').write_text(
        'import pytest\n'
        '@pytest.mark.parametrize("value", [1, 2])\n'
        'def test_positive(value): assert value > 0\n', encoding='utf-8')
    (suite / 'test_two.py').write_text(
        'import pytest\n@pytest.mark.skip(reason="existing skip")\ndef test_skipped(): pass\n',
        encoding='utf-8')
    env = os.environ.copy()
    env.update(PYTHONPATH=str(root), PYTEST_PLUGINS='tools.ci.unit_shard_selection',
               MJOBS_CI_UNIT_SHARD_INDEX='0', MJOBS_CI_UNIT_SHARD_COUNT='1')
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests/unit',
                             '--junitxml=outputs/ci/unit.xml'],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    output = tmp_path / 'outputs/ci'
    manifest = json.loads((output / 'unit-collection.json').read_text(encoding='utf-8'))
    assert manifest['inventory'] == manifest['selected'] == manifest['completed']
    assert reconcile(output, 1, output / 'unit.xml', single=True) == 3
    assert len(list(ET.parse(output / 'unit.xml').iter('testcase'))) == 3
