"""Pytest plugin for exact node exclusion in the isolated integration matrix."""
import json
import os


def pytest_collection_modifyitems(config, items):
    excluded = set(json.loads(os.environ.get('MJOBS_CI_EXCLUDE_NODES', '[]')))
    if not excluded:
        return
    removed = [item for item in items if item.nodeid in excluded]
    if {item.nodeid for item in removed} != excluded:
        raise ValueError('Expected split test was not collected')
    items[:] = [item for item in items if item.nodeid not in excluded]
    config.hook.pytest_deselected(items=removed)
