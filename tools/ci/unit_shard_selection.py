"""Opt-in CI-only isolated unit scheduling with fail-closed execution evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.ci.run_shard import weighted_groups

PLUGIN = 'tools.ci.unit_shard_selection'


def split_inventory(nodeids: list[str], count: int, timings: dict[str, float]) -> list[list[str]]:
    """Assign complete files, keeping all collected cases, including newly added ones."""
    if count < 1 or len(set(nodeids)) != len(nodeids):
        raise ValueError('Invalid unit shard inventory')
    by_file: dict[str, list[str]] = {}
    for nodeid in nodeids:
        file = nodeid.split('::', 1)[0]
        if not file.startswith('tests/unit/') or not file.endswith('.py'):
            raise ValueError(f'Unexpected unit test path: {file}')
        by_file.setdefault(file, []).append(nodeid)
    groups = weighted_groups(sorted(by_file), count, timings)
    result = [sorted(node for file in group for node in by_file[file]) for group in groups]
    if not all(result):
        raise ValueError('Empty unit shard must not pass')
    return result


class UnitShard:
    def __init__(self, index: int, count: int):
        if not 0 <= index < count:
            raise ValueError('Invalid unit shard index')
        self.index = index
        self.count = count
        self.inventory: list[str] = []
        self.selected: list[str] = []
        self.completed: set[str] = set()

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        self.inventory = sorted(item.nodeid for item in items)
        profile = Path(config.rootpath) / 'tools/ci/unit_timings.json'
        timings = json.loads(profile.read_text(encoding='utf-8'))['seconds'] if profile.is_file() else {}
        self.selected = split_inventory(self.inventory, self.count, timings)[self.index]
        selected = set(self.selected)
        removed = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        config.hook.pytest_deselected(items=removed)

    def pytest_collection_finish(self, session):
        if sorted(item.nodeid for item in session.items) != self.selected:
            raise ValueError('Final unit collection does not match assigned shard')

    def pytest_runtest_logreport(self, report):
        if report.when == 'teardown':
            self.completed.add(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        if exitstatus != pytest.ExitCode.OK:
            return
        if not self.selected or self.completed != set(self.selected):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            return
        output = Path(session.config.rootpath) / 'outputs/ci/unit-collection.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            'index': self.index,
            'count': self.count,
            'inventory': self.inventory,
            'selected': self.selected,
            'completed': sorted(self.completed),
        }) + '\n', encoding='utf-8')


def pytest_configure(config):
    index = int(os.environ.pop('MJOBS_CI_UNIT_SHARD_INDEX'))
    count = int(os.environ.pop('MJOBS_CI_UNIT_SHARD_COUNT'))
    # Child pytest invocations inside tests must not inherit CI-only selection.
    remaining = [name for name in os.environ.get('PYTEST_PLUGINS', '').split(',')
                 if name and name != PLUGIN]
    if remaining:
        os.environ['PYTEST_PLUGINS'] = ','.join(remaining)
    else:
        os.environ.pop('PYTEST_PLUGINS', None)
    (Path(config.rootpath) / 'outputs/ci/unit-collection.json').unlink(missing_ok=True)
    config.pluginmanager.register(UnitShard(index, count), 'mjobs-unit-shard')
