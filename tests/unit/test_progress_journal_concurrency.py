"""Cooperating journal publishers must not lose successful checkpoints."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

from tests.unit import test_progress_journal_recovery as recovery


LOCK_NAME = ".implementation-progress.lock"
BUSY = "Implementation journal is busy; retry after the active writer finishes"
HOLDER = """
import json, sys, time
from pathlib import Path
from tools.verification import progress
state, target, ready, release = map(Path, sys.argv[1:5])
operation = sys.argv[5]
entry = json.loads(sys.argv[6])
write = progress.write_atomic_json
def pause(path, data):
    if Path(path).resolve() == target.resolve():
        ready.write_text("snapshot-read", encoding="utf-8")
        deadline = time.monotonic() + 12
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned test publisher barrier expired")
            time.sleep(.01)
    return write(path, data)
progress.write_atomic_json = pause
if operation == "record":
    progress.append_milestone(state, entry)
else:
    progress.sync_implementation_state(state.parent.parent)
"""
CONTENDER = """
import json, sys
from pathlib import Path
from tools.verification import progress
progress.JOURNAL_LOCK_TIMEOUT_SECONDS = .2
state = Path(sys.argv[1])
try:
    if sys.argv[2] == "record":
        progress.append_milestone(state, json.loads(sys.argv[3]))
    else:
        progress.sync_implementation_state(state.parent.parent)
except TimeoutError as error:
    print(str(error))
    raise SystemExit(3)
"""


class ProgressJournalConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery.ProgressJournalRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.state = self.fixture.state
        self.output = self.fixture.output

    def entry(self, identifier):
        return {
            **self.fixture.entry, "milestone_id": identifier,
            "timestamp": "2026-11-03T00:01:00Z", "current_phase": identifier,
        }

    def start_holder(self, operation, target, entry=None):
        ready, release = self.root / "publisher-ready", self.root / "publisher-release"
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", HOLDER, str(self.state), str(target),
             str(ready), str(release), operation, json.dumps(entry or self.entry("A"))],
            cwd=self.root, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def cleanup():
            release.write_text("continue", encoding="utf-8")
            if child.poll() is None:
                try:
                    child.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()  # This exact child was created and retained by this test.
                    child.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 8
        while not ready.exists():
            if child.poll() is not None:
                out, err = child.communicate()
                self.fail(f"Owned publisher exited before its barrier: {out} {err}")
            if time.monotonic() >= deadline:
                self.fail("Owned publisher did not reach its bounded barrier")
            time.sleep(.01)
        return child, release

    def finish_holder(self, holder):
        child, release = holder
        release.write_text("continue", encoding="utf-8")
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, out + err)

    def contender(self, operation, entry=None):
        return subprocess.run(
            [sys.executable, "-B", "-c", CONTENDER, str(self.state), operation,
             json.dumps(entry or self.entry("B"))],
            cwd=self.root, text=True, encoding="utf-8", capture_output=True,
            check=False, timeout=8,
        )

    def assert_busy_without_writes(self, operation, entry=None):
        before = self.state.read_bytes(), self.output.read_bytes()
        started = time.monotonic()
        result = self.contender(operation, entry)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), BUSY)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)

    def assert_history(self, identifiers):
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual([entry["milestone_id"] for entry in state["milestone_journal"]],
                         identifiers)
        self.fixture.assert_current_output()

    def test_competing_record_refuses_without_losing_success_and_can_retry(self):
        holder = self.start_holder("record", self.state, self.entry("A"))
        self.assert_busy_without_writes("record", self.entry("B"))
        self.finish_holder(holder)
        self.fixture.record(self.entry("B"))
        self.assert_history(["CHECKPOINT-1", "A", "B"])

    def test_sync_cannot_publish_inside_an_unfinished_record(self):
        holder = self.start_holder("record", self.output, self.entry("A"))
        self.assert_busy_without_writes("sync")
        self.finish_holder(holder)
        result = self.fixture.cli("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_history(["CHECKPOINT-1", "A"])

    def test_record_cannot_overtake_an_older_sync_snapshot(self):
        holder = self.start_holder("sync", self.output)
        self.assert_busy_without_writes("record", self.entry("B"))
        self.finish_holder(holder)
        self.fixture.record(self.entry("B"))
        self.assert_history(["CHECKPOINT-1", "B"])

    def test_coordination_file_is_refused_as_direct_or_normalized_output(self):
        lock = self.state.parent / LOCK_NAME
        entry_path = self.root / "candidate-entry.json"
        entry_path.write_text(json.dumps(self.entry("B")), encoding="utf-8")
        for destination, target in (("direct", lock),
                                    ("normalized", self.root / "planning/../planning" / LOCK_NAME)):
            for command in ("sync", "record"):
                with self.subTest(command=command, destination=destination):
                    before = self.state.read_bytes(), self.output.read_bytes()
                    original_lock = lock.read_bytes() if lock.exists() else None
                    try:
                        args = (["sync", "--output", target] if command == "sync" else
                                ["record", "--entry", entry_path, "--output-path", target])
                        result = self.fixture.cli(*args)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("Output journal must be separate from coordination lock",
                                      result.stderr)
                        self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)
                        self.assertEqual(lock.read_bytes() if lock.exists() else None, original_lock)
                    finally:
                        self.state.write_bytes(before[0])
                        self.output.write_bytes(before[1])
                        if original_lock is None:
                            lock.unlink(missing_ok=True)
                        else:
                            lock.write_bytes(original_lock)

    def test_hardlinked_coordination_file_is_refused_without_touching_other_bytes(self):
        lock = self.state.parent / LOCK_NAME
        lock.unlink(missing_ok=True)
        other = self.root / "unrelated-owned-file.txt"
        other.write_bytes(b"unrelated-owned-by-fixture")
        os.link(other, lock)
        before = self.state.read_bytes(), self.output.read_bytes(), other.read_bytes()
        result = self.contender("record")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Progress coordination lock must be a single-link regular file",
                      result.stderr)
        self.assertEqual((self.state.read_bytes(), self.output.read_bytes(),
                          other.read_bytes()), before)

    @unittest.skipIf(os.name == "nt", "POSIX symlink creation without Windows elevation")
    def test_symlink_coordination_file_is_refused_without_touching_target(self):
        lock = self.state.parent / LOCK_NAME
        lock.unlink(missing_ok=True)
        other = self.root / "symlink-target.txt"
        other.write_bytes(b"unrelated-owned-by-fixture")
        lock.symlink_to(other)
        before = self.state.read_bytes(), self.output.read_bytes(), other.read_bytes()
        result = self.contender("record")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Progress coordination lock cannot be a symbolic link", result.stderr)
        self.assertEqual((self.state.read_bytes(), self.output.read_bytes(),
                          other.read_bytes()), before)

    def test_an_unrelated_journal_is_not_blocked_by_the_owned_publisher(self):
        holder = self.start_holder("record", self.state, self.entry("A"))
        other = recovery.ProgressJournalRecoveryTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        other.record({**other.entry, "milestone_id": "OTHER",
                      "timestamp": "2026-11-03T00:01:00Z"})
        other.assert_current_output()
        self.finish_holder(holder)
        self.assert_history(["CHECKPOINT-1", "A"])


if __name__ == "__main__":
    unittest.main()
