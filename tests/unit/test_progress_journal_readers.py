"""Checkpoint readers must observe one cooperating publication at a time."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest

from tests.unit import test_progress_journal_concurrency as concurrency
from tools.verification import progress


READER = """
import sys
from tools.verification import progress
progress.JOURNAL_LOCK_TIMEOUT_SECONDS = .2
sys.argv = ["progress", *sys.argv[1:]]
try:
    raise SystemExit(progress.main())
except TimeoutError as error:
    print(str(error))
    raise SystemExit(3)
"""
WAITING_READER = """
import sys
from pathlib import Path
from tools.verification import progress
ready = Path(sys.argv[1])
lock = progress._set_journal_lock
def observe_lock(handle, *, unlock=False):
    try:
        return lock(handle, unlock=unlock)
    except OSError:
        ready.write_text("contended", encoding="utf-8")
        raise
progress._set_journal_lock = observe_lock
sys.argv = ["progress", *sys.argv[2:]]
raise SystemExit(progress.main())
"""
PAUSED_READER = """
import json, sys, time
from pathlib import Path
from tools.verification import progress
state, ready, release = map(Path, sys.argv[1:4])
read = Path.read_text
paused = False
def pause(path, *args, **kwargs):
    global paused
    text = read(path, *args, **kwargs)
    if path.resolve() == state.resolve() and not paused:
        paused = True
        ready.write_text("snapshot-read", encoding="utf-8")
        deadline = time.monotonic() + 12
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned reader barrier expired")
            time.sleep(.01)
    return text
Path.read_text = pause
print(json.dumps(progress.resume_snapshot(state.parent.parent)))
"""


class ProgressJournalReaderTests(unittest.TestCase):
    def setUp(self):
        self.publisher = concurrency.ProgressJournalConcurrencyTests("runTest")
        self.addCleanup(self.publisher.doCleanups)
        self.publisher.setUp()
        self.fixture = self.publisher.fixture
        self.root = self.fixture.root
        self.state = self.fixture.state
        self.output = self.fixture.output

    def read_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", "-c", READER, *map(str, arguments)],
            cwd=self.root, text=True, encoding="utf-8", capture_output=True,
            check=False, timeout=8,
        )

    def commands(self):
        return (("validate",), ("validate", "--state", self.state),
                ("resume",), ("resume", "--source", self.state),
                ("resume", "--source", self.output))

    def start_reader(self, script, ready, *arguments):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", script, *map(str, arguments)],
            cwd=self.root, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def cleanup():
            if child.poll() is None:
                child.kill()  # Only the exact child created by this fixture.
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 8
        while not ready.exists():
            if child.poll() is not None:
                out, err = child.communicate()
                self.fail(f"Owned reader exited before its barrier: {out} {err}")
            if time.monotonic() >= deadline:
                self.fail("Owned reader did not reach its bounded barrier")
            time.sleep(.01)
        return child

    def test_busy_readers_refuse_without_misclassifying_or_changing_journals(self):
        holder = self.publisher.start_holder("record", self.output)
        before = self.state.read_bytes(), self.output.read_bytes()
        for command in self.commands():
            with self.subTest(command=tuple(map(str, command))):
                started = time.monotonic()
                result = self.read_cli(*command)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), concurrency.BUSY)
                self.assertEqual(result.stderr, "")
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)
        self.publisher.finish_holder(holder)
        for command in self.commands():
            with self.subTest(retry=tuple(map(str, command))):
                result = self.read_cli(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if command[0] == "resume":
                    self.assertEqual(json.loads(result.stdout)["latest_milestone"]["milestone_id"], "A")

    def test_waiting_resume_reads_the_completed_publication(self):
        holder = self.publisher.start_holder("record", self.output)
        ready = self.root / "reader-waiting"
        reader = self.start_reader(WAITING_READER, ready, ready, "resume")
        self.publisher.finish_holder(holder)
        out, err = reader.communicate(timeout=8)
        self.assertEqual(reader.returncode, 0, out + err)
        self.assertEqual(json.loads(out)["latest_milestone"]["milestone_id"], "A")
        self.publisher.assert_history(["CHECKPOINT-1", "A"])

    def test_reader_keeps_its_snapshot_stable_until_validation_finishes(self):
        ready, release = self.root / "reader-ready", self.root / "reader-release"
        reader = self.start_reader(PAUSED_READER, ready, self.state, ready, release)
        self.publisher.assert_busy_without_writes("record", self.publisher.entry("B"))
        release.write_text("continue", encoding="utf-8")
        out, err = reader.communicate(timeout=8)
        self.assertEqual(reader.returncode, 0, out + err)
        self.assertEqual(json.loads(out)["latest_milestone"], self.fixture.entry)
        self.fixture.record(self.publisher.entry("B"))
        self.publisher.assert_history(["CHECKPOINT-1", "B"])

    def test_output_only_recovery_does_not_require_or_create_a_planning_directory(self):
        self.state.unlink()
        (self.state.parent / concurrency.LOCK_NAME).unlink()
        self.state.parent.rmdir()
        before = self.output.read_bytes()
        for command in (("resume",), ("resume", "--source", self.output)):
            with self.subTest(command=tuple(map(str, command))):
                result = self.read_cli(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["latest_milestone"]["milestone_id"],
                                 "CHECKPOINT-1")
                self.assertEqual(self.output.read_bytes(), before)
                self.assertFalse(self.state.parent.exists())

    def test_reader_lock_releases_after_invalid_history_is_refused(self):
        original = self.state.read_bytes()
        state = json.loads(original)
        state["current_phase"] = "INVALID"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        for command in (("validate",), ("resume",)):
            result = self.read_cli(*command)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Current phase disagrees", result.stdout + result.stderr)
        self.state.write_bytes(original)
        self.fixture.record(self.publisher.entry("B"))
        self.publisher.assert_history(["CHECKPOINT-1", "B"])

    def test_unrelated_journal_readers_are_not_blocked(self):
        holder = self.publisher.start_holder("record", self.output)
        other = concurrency.recovery.ProgressJournalRecoveryTests("runTest")
        self.addCleanup(other.doCleanups)
        other.setUp()
        self.assertEqual(progress.validate_journal(other.root), [])
        self.assertEqual(progress.resume_snapshot(other.root)["latest_milestone"], other.entry)
        self.publisher.finish_holder(holder)


if __name__ == "__main__":
    unittest.main()
