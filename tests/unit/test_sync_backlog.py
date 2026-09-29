"""Catch lost checkpoints and unsafe duplicate issue synchronization."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sync_backlog", Path(__file__).resolve().parents[2] / "bootstrap-tools/sync_backlog.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class BacklogTests(unittest.TestCase):
    def test_duplicate_remote_markers_are_not_silently_overwritten(self):
        rows = [{"number": 1, "body": "<!-- mj-requirement:A -->"},
                {"number": 2, "body": "<!-- mj-requirement:A -->"}]
        with patch.object(sync, "gh_json", return_value=rows), self.assertRaises(SystemExit):
            sync.existing("owner/repo")

    def test_external_process_timeout_is_bounded_and_classified(self):
        def subprocess_run(args, **kwargs):
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 120)
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        with patch.object(sync.subprocess, "run", side_effect=subprocess_run), self.assertRaisesRegex(SystemExit, "TIMEOUT"):
            sync.run(["gh", "api", "test"])

    def test_atomic_checkpoint_can_be_read_after_each_acknowledged_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "map.json"
            sync.write_map("owner/repo", {"A": 17}, path=target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["issues"], {"A": 17})
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_second_local_sync_cannot_acquire_same_mutation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync.lock"
            with sync.mutation_lock(path):
                with self.assertRaisesRegex(SystemExit, "LOCKED"):
                    with sync.mutation_lock(path):
                        self.fail("second lock acquired")
            self.assertFalse(path.exists())

    def test_failed_creation_retains_acknowledged_map_and_pending_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "map.json"
            with patch.object(sync, "existing", return_value={}), patch.object(
                sync, "create_issue", side_effect=[17, SystemExit("network failure")]
            ), self.assertRaises(SystemExit):
                sync.synchronize("owner/repo", [{"id": "A"}, {"id": "B"}], path=target, delay=0)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["issues"], {"A": 17})
            self.assertEqual(data["pending_creation"], "B")

    def test_ambiguous_previous_creation_is_not_blindly_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "map.json"
            target.write_text(json.dumps({"repository": "owner/repo", "issues": {}, "pending_creation": "B"}), encoding="utf-8")
            with patch.object(sync, "existing", return_value={}), patch.object(sync, "ensure_labels") as labels, self.assertRaisesRegex(SystemExit, "UNCONFIRMED"):
                sync.synchronize("owner/repo", [{"id": "B"}], path=target, delay=0, labels=True)
            labels.assert_not_called()

    def test_dead_owner_lock_can_be_recovered_after_explicit_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync.lock"
            path.write_text("123456", encoding="utf-8")
            with patch.object(sync, "pid_alive", return_value=False):
                with sync.mutation_lock(path, recover=True):
                    self.assertTrue(path.exists())
            self.assertFalse(path.exists())

    def test_recovery_never_removes_live_owner_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync.lock"
            path.write_text("123456", encoding="utf-8")
            with patch.object(sync, "pid_alive", return_value=True), self.assertRaisesRegex(SystemExit, "LOCKED"):
                with sync.mutation_lock(path, recover=True):
                    self.fail("live owner replaced")
            self.assertEqual(path.read_text(encoding="utf-8"), "123456")

    def test_existing_labels_are_preserved(self):
        definitions = json.loads(sync.LABELS.read_text(encoding="utf-8"))
        with patch.object(sync, "gh_json", return_value=[{"name": row["name"]} for row in definitions]), patch.object(sync, "run") as command:
            sync.ensure_labels("owner/repo")
        command.assert_not_called()

    def test_malformed_existing_map_is_preserved_and_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(sync, "existing", return_value={}), self.assertRaisesRegex(SystemExit, "INVALID_MAP"):
                sync.synchronize("owner/repo", [], path=path, delay=0)
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
