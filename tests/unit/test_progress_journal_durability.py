"""Journal publication must surface a failed POSIX directory durability barrier."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tests.unit import test_progress_journal_recovery as recovery
from tools.verification import progress


class ProgressJournalDurabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mjobs-journal-durability-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "checkpoint.json"
        self.target.write_text('{"checkpoint": "prior"}\n', encoding="utf-8")
        self.updated = {"checkpoint": "next", "note": "Synthetic checkpoint"}

    def assert_no_staging_files(self):
        self.assertEqual(list(self.root.glob(".checkpoint-*.tmp")), [])

    def test_failed_content_flush_preserves_previous_checkpoint(self):
        before = self.target.read_bytes()
        with patch.object(progress.os, "fsync", side_effect=OSError(errno.EIO, "fixture flush failure")):
            with self.assertRaises(OSError):
                progress.write_atomic_json(self.target, self.updated)
        self.assertEqual(self.target.read_bytes(), before)
        self.assert_no_staging_files()

    @unittest.skipUnless(os.name == "posix", "POSIX directory descriptors required")
    def test_success_flushes_contents_then_replacement_directory(self):
        events = []
        real_sync, real_replace = os.fsync, os.replace
        parent = self.root.stat()

        def observe_sync(descriptor):
            metadata = os.fstat(descriptor)
            if stat.S_ISDIR(metadata.st_mode):
                self.assertEqual((metadata.st_dev, metadata.st_ino),
                                 (parent.st_dev, parent.st_ino))
                self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), self.updated)
                events.append("directory")
            else:
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")),
                                 {"checkpoint": "prior"})
                events.append("contents")
            return real_sync(descriptor)

        def observe_replace(source, target):
            real_replace(source, target)
            events.append("replacement")

        with patch.object(progress.os, "fsync", side_effect=observe_sync), \
                patch.object(progress.os, "replace", side_effect=observe_replace):
            progress.write_atomic_json(self.target, self.updated)
        self.assertEqual(events, ["contents", "replacement", "directory"])
        self.assert_no_staging_files()

    @unittest.skipUnless(os.name == "posix", "POSIX directory descriptors required")
    def test_failed_directory_flush_reports_uncertain_publication_and_closes_descriptor(self):
        descriptors = []
        real_sync = os.fsync

        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                descriptors.append(descriptor)
                raise OSError(errno.EIO, "fixture directory flush failure")
            return real_sync(descriptor)

        with patch.object(progress.os, "fsync", side_effect=fail_directory):
            with self.assertRaisesRegex(OSError, "fixture directory flush failure"):
                progress.write_atomic_json(self.target, self.updated)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        # Replacement precedes its durability barrier: never pretend failure
        # means the old milestone is still present or blindly append it again.
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), self.updated)
        self.assert_no_staging_files()
        progress.write_atomic_json(self.target, self.updated)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), self.updated)

    @unittest.skipUnless(os.name == "posix", "POSIX directory descriptors required")
    def test_unavailable_directory_flush_is_not_silently_downgraded(self):
        real_open = os.open

        def refuse_parent(path, flags, *args, **kwargs):
            if Path(path) == self.root:
                raise OSError(errno.EACCES, "fixture directory inaccessible")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(progress.os, "open", side_effect=refuse_parent):
            with self.assertRaisesRegex(OSError, "fixture directory inaccessible"):
                progress.write_atomic_json(self.target, self.updated)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), self.updated)
        self.assert_no_staging_files()

    @unittest.skipIf(os.name == "posix", "Native non-POSIX compatibility control")
    def test_non_posix_retains_file_flush_and_atomic_replacement(self):
        real_sync = os.fsync
        synced = []

        def observe_file(descriptor):
            self.assertTrue(stat.S_ISREG(os.fstat(descriptor).st_mode))
            synced.append(descriptor)
            return real_sync(descriptor)

        with patch.object(progress.os, "fsync", side_effect=observe_file):
            progress.write_atomic_json(self.target, self.updated)
        self.assertEqual(len(synced), 1)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), self.updated)
        self.assert_no_staging_files()


@unittest.skipUnless(os.name == "posix", "POSIX directory descriptors required")
class ProgressJournalDurabilityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery.ProgressJournalRecoveryTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_failed_canonical_directory_flush_recovers_without_duplicate_milestone(self):
        fixture = self.fixture
        next_entry = {**fixture.entry, "milestone_id": "CHECKPOINT-2",
                      "timestamp": "2026-11-03T00:01:00Z", "current_phase": "SECOND"}
        prior_output = fixture.output.read_bytes()
        real_sync = os.fsync
        canonical_parent = fixture.state.parent.stat()

        def fail_canonical_directory(descriptor):
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == (canonical_parent.st_dev, canonical_parent.st_ino):
                raise OSError(errno.EIO, "fixture canonical directory flush failure")
            return real_sync(descriptor)

        with patch.object(progress.os, "fsync", side_effect=fail_canonical_directory):
            with self.assertRaisesRegex(OSError, "fixture canonical directory flush failure"):
                progress.append_milestone(fixture.state, next_entry)
        canonical = fixture.state.read_bytes()
        self.assertEqual(json.loads(canonical)["milestone_journal"], [fixture.entry, next_entry])
        self.assertEqual(fixture.output.read_bytes(), prior_output)
        self.assertTrue(progress.validate_journal(fixture.root))
        repaired = fixture.cli("sync")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(fixture.state.read_bytes(), canonical)
        fixture.assert_current_output()
        resumed = fixture.cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["latest_milestone"], next_entry)
        self.assertEqual(list(fixture.state.parent.glob("*.tmp")), [])

    def test_sync_reflushes_canonical_directory_before_repairing_derived_copy(self):
        fixture = self.fixture
        fixture.interrupt_after_canonical_replace()
        before = fixture.state.read_bytes(), fixture.output.read_bytes()
        parent_info = {"canonical": fixture.state.parent.stat(),
                       "derived": fixture.output.parent.stat()}
        real_sync = os.fsync
        events = []

        def observe_sync(descriptor):
            info = os.fstat(descriptor)
            for label, expected in parent_info.items():
                if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                    events.append(label)
                    if label == "canonical":
                        self.assertEqual((fixture.state.read_bytes(), fixture.output.read_bytes()), before)
            return real_sync(descriptor)

        with patch.object(progress.os, "fsync", side_effect=observe_sync):
            progress.sync_implementation_state(fixture.root)
        self.assertEqual(events, ["canonical", "derived"])
        self.assertEqual(fixture.state.read_bytes(), before[0])
        fixture.assert_current_output()

    def test_sync_cannot_report_success_while_canonical_directory_flush_fails(self):
        fixture = self.fixture
        fixture.interrupt_after_canonical_replace()
        before = fixture.state.read_bytes(), fixture.output.read_bytes()
        canonical_parent = fixture.state.parent.stat()
        real_sync = os.fsync

        def fail_canonical_directory(descriptor):
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == (canonical_parent.st_dev, canonical_parent.st_ino):
                raise OSError(errno.EIO, "fixture recovery durability failure")
            return real_sync(descriptor)

        with patch.object(progress.os, "fsync", side_effect=fail_canonical_directory):
            with self.assertRaisesRegex(OSError, "fixture recovery durability failure"):
                progress.sync_implementation_state(fixture.root)
        self.assertEqual((fixture.state.read_bytes(), fixture.output.read_bytes()), before)
        repaired = fixture.cli("sync")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        fixture.assert_current_output()
