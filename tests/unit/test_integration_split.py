"""Exercise exact-node scheduling with real collection and synthetic JUnit evidence."""

import pytest

from tools.ci.integration_split import (
    RECOVERY_CASES,
    RECOVERY_CLASS,
    RECOVERY_FILE,
    SPLIT_NODES,
    invocation,
    target_weights,
)
from tools.ci.integration_timing_profile import complete_profile, report_timings
from tools.ci.run_shard import weighted_groups


def test_split_assigns_every_target_once():
    files = [RECOVERY_FILE, 'tests/integration/test_other.py']
    targets, weights = target_weights(files, {RECOVERY_FILE: 305, files[1]: 6}, {})
    assert sum(weights[t] for t in targets) == 311
    groups = weighted_groups(targets, 3, weights)
    assert sorted(t for group in groups for t in group) == sorted(targets)
    assert not any(set(SPLIT_NODES) <= set(group) for group in groups)


def test_invocation_does_not_duplicate_or_drop_new_file_cases():
    assert invocation([RECOVERY_FILE, SPLIT_NODES[0]]) == (
        [RECOVERY_FILE], [SPLIT_NODES[1]])
    assert invocation([RECOVERY_FILE]) == ([RECOVERY_FILE], list(SPLIT_NODES))
    assert invocation(list(SPLIT_NODES)) == (list(SPLIT_NODES), [])
    assert invocation([RECOVERY_FILE, *SPLIT_NODES]) == ([RECOVERY_FILE], [])


def test_profile_aggregates_split_reports_and_rejects_missing_case(tmp_path):
    module = RECOVERY_FILE.removesuffix('.py').replace('/', '.')
    reports = []
    for i, (cls, name, seconds) in enumerate([
        ('OtherTests', 'test_future', 1), (RECOVERY_CLASS, RECOVERY_CASES[0], 152),
        (RECOVERY_CLASS, RECOVERY_CASES[1], 151),
    ]):
        path = tmp_path / f'integration-{i}.xml'
        path.write_text(f'<testsuite><testcase classname="{module}.{cls}" name="{name}" '
                        f'time="{seconds}"/></testsuite>', encoding='utf-8')
        reports.append(path)
    assert complete_profile([RECOVERY_FILE], reports) == {RECOVERY_FILE: 304}
    assert report_timings(reports[1], [SPLIT_NODES[0]], split=True) == {SPLIT_NODES[0]: 152}
    with pytest.raises(ValueError, match='Missing test files'):
        complete_profile([RECOVERY_FILE], reports[:2])
    with pytest.raises(ValueError, match='multiple shard reports'):
        complete_profile([RECOVERY_FILE], [*reports, reports[1]])

def test_exact_pytest_node_exclusion_preserves_future_prefix_matches(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tools.ci.shard_selection import pytest_collection_modifyitems

    node = SPLIT_NODES[0]
    items = [SimpleNamespace(nodeid=x) for x in (node, node + '_future', SPLIT_NODES[1])]
    hook = Mock()
    config = SimpleNamespace(hook=hook)
    monkeypatch.setenv('MJOBS_CI_EXCLUDE_NODES', __import__('json').dumps([node]))
    pytest_collection_modifyitems(config, items)
    assert [item.nodeid for item in items] == [node + '_future', SPLIT_NODES[1]]
    hook.pytest_deselected.assert_called_once()
    with pytest.raises(ValueError, match='not collected'):
        pytest_collection_modifyitems(config, items)
