"""Record the exact pytest collection and terminal reports for one assigned shard."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _pytest.junitxml import bin_xml_escape, mangle_test_address


class ShardCollection:
    def __init__(self, output: Path, excluded: list[str]):
        self.output = output
        self.excluded = excluded
        self.inventory: list[str] = []
        self.selected: list[dict[str, str]] = []
        self.completed: list[str] = []

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        self.inventory = sorted(item.nodeid for item in items)
        yield
        expected = set(self.inventory) - set(self.excluded)
        selected = sorted(item.nodeid for item in items)
        if (len(set(self.inventory)) != len(self.inventory)
                or len(set(self.excluded)) != len(self.excluded)
                or not set(self.excluded) <= set(self.inventory)
                or selected != sorted(expected) or not selected):
            raise pytest.UsageError("Unexpected collection, deselection, or empty shard")
        for node in selected:
            names = mangle_test_address(node)
            self.selected.append({
                "nodeid": node,
                "classname": ".".join(names[:-1]),
                "name": bin_xml_escape(names[-1]),
            })

    def pytest_collection_finish(self, session):
        if sorted(item.nodeid for item in session.items) != [row["nodeid"] for row in self.selected]:
            raise pytest.UsageError("Final collection does not match shard evidence")

    def pytest_runtest_logreport(self, report):
        if report.when == "teardown":
            self.completed.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        if exitstatus != pytest.ExitCode.OK:
            return
        if not self.selected or sorted(self.completed) != [row["nodeid"] for row in self.selected]:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            return
        self.output.write_text(json.dumps({
            "inventory": self.inventory,
            "excluded": self.excluded,
            "selected": self.selected,
            "completed": sorted(self.completed),
        }) + "\n", encoding="utf-8")


def pytest_configure(config):
    output = Path(os.environ.pop("MJOBS_CI_SHARD_COLLECTION_PATH"))
    excluded = json.loads(os.environ.get("MJOBS_CI_EXCLUDE_NODES", "[]"))
    output.unlink(missing_ok=True)
    config.pluginmanager.register(ShardCollection(output, excluded), "mjobs-shard-collection")
