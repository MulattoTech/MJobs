"""Restore consumes one verified snapshot across Docker operation boundaries."""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from cli import local_backup
from tests.unit import test_local_backup as fixtures
from tests.unit.test_backup_fixture_receipts import capture_fixture, RECEIPTS


@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction boundary")
@pytest.mark.parametrize("linked_component", ["archive", "ancestor", "documents"])
def test_native_junction_rejected_before_read_or_target_operation(linked_component):
    import _winapi

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        target.mkdir()
        archive = target / "archive"
        archive.mkdir()
        manifest = fixtures.LocalBackupTests.archive_fixture(archive)
        if linked_component == "documents":
            original_documents = archive / "documents"
            destination = target / "original-documents"
            assert destination.absolute().is_relative_to(root.absolute())
            original_documents.rename(destination)
            link = original_documents
            selected = archive
        else:
            destination = archive if linked_component == "archive" else target
            link = root / "selected"
            selected = link if linked_component == "archive" else link / "archive"
        assert link.absolute().is_relative_to(root.absolute())
        assert destination.absolute().is_relative_to(root.absolute())
        _winapi.CreateJunction(str(destination), str(link))
        try:
            assert link.is_junction()
            with patch.object(local_backup, "checksum", wraps=local_backup.checksum) as checksum, \
                 patch.object(local_backup, "_compose") as compose:
                with pytest.raises(local_backup.BackupError):
                    local_backup.verify_backup(selected)
                with pytest.raises(local_backup.BackupError):
                    local_backup.restore_backup(selected, "synthetic-restored", 18712)
                assert not checksum.called and not compose.called
            if linked_component == "documents":
                with pytest.raises(local_backup.BackupError):
                    local_backup.verify_document_tree(manifest, archive)
        finally:
            link.rmdir()  # Remove only the junction, preserving its target.
        if linked_component == "documents":
            destination.rename(archive / "documents")
        assert local_backup.verify_backup(archive) == manifest


@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction boundary")
def test_capture_refuses_destination_junction_ancestor_before_any_operation():
    import _winapi

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        target.mkdir()
        link = root / "selected"
        assert link.absolute().is_relative_to(root.absolute())
        assert target.absolute().is_relative_to(root.absolute())
        _winapi.CreateJunction(str(target), str(link))
        try:
            with patch.object(local_backup, "_config", side_effect=AssertionError("backup started")) as config:
                with pytest.raises(local_backup.BackupError):
                    local_backup.create_backup("synthetic-source", link / "new-backup")
            assert not config.called
            assert list(target.iterdir()) == []
        finally:
            link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction boundary")
def test_capture_rejects_document_junction_and_preserves_its_target():
    import _winapi

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "unrelated-documents"
        target.mkdir()
        sentinel = target / "private.txt"
        sentinel.write_bytes(b"synthetic-private-document")
        archive = root / "backup"
        states = {service: "exited" for service in local_backup.WRITERS}
        states["postgres"] = "running"
        links = []

        def compose(*_args, stdout=None, **_kwargs):
            if stdout is not None:
                stdout.write(b"synthetic-database")
            return b""

        def copy_documents(command, **_kwargs):
            link = Path(command[-1]) / "linked-documents"
            assert link.absolute().is_relative_to(root.absolute())
            assert target.absolute().is_relative_to(root.absolute())
            _winapi.CreateJunction(str(target), str(link))
            links.append(link)
            return b""

        with patch.object(local_backup, "_config", return_value={}), \
             patch.object(local_backup, "_service_states", return_value=states), \
             patch.object(local_backup, "_container", return_value="synthetic-api"), \
             patch.object(local_backup, "_sql", side_effect=("PERSONAL", "synthetic-revision")), \
             patch.object(local_backup, "_compose", side_effect=compose), \
             patch.object(local_backup, "_run", side_effect=copy_documents):
            with pytest.raises(local_backup.BackupError, match="unsupported"):
                local_backup.create_backup("synthetic-source", archive)
        assert links and not any(link.exists() for link in links)
        assert sentinel.read_bytes() == b"synthetic-private-document"
        assert not archive.exists()
        assert sorted(path.name for path in root.iterdir()) == ["unrelated-documents"]


def test_restore_uses_original_database_documents_and_manifest_after_source_changes():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "archive"
        archive.mkdir()
        manifest = fixtures.LocalBackupTests.archive_fixture(archive)
        observed = {}
        def config(*_args):
            (archive / "database.dump").write_bytes(b"unverified dump")
            (archive / "documents" / "resume.pdf").write_bytes(b"unverified document")
            (archive / "documents" / "extra.txt").write_bytes(b"unlisted")
            (archive / "manifest.json").write_text("{}")
            return {}
        def compose(_project, *arguments, **kwargs):
            stream = kwargs.get("stdin")
            if "pg_restore" in arguments:
                observed["database"] = stream.read()
            if "verify-documents" in arguments:
                observed["manifest"] = json.load(stream)
            return b""
        def run(arguments, **_kwargs):
            if arguments[:2] == ["docker", "cp"]:
                source = Path(arguments[2])
                observed["documents"] = {p.name: p.read_bytes() for p in source.iterdir()}
                observed["snapshot"] = source.parent
            return b""
        with patch.object(local_backup, "_config", side_effect=config), \
             patch.object(local_backup, "_compose", side_effect=compose), \
             patch.object(local_backup, "_run", side_effect=run), \
             patch.object(local_backup, "_sql", side_effect=("0", "synthetic-revision", "PERSONAL")), \
             patch.object(local_backup, "_volumes", return_value=[]), \
             patch.object(local_backup, "_container", return_value="synthetic-api"), \
             patch.object(local_backup, "_wait_ready"):
            result = local_backup.restore_backup(archive, "synthetic-restored", 18712)
        assert observed["database"] == b"synthetic-postgres-dump"
        assert observed["documents"] == {"resume.pdf": b"synthetic-document"}
        assert observed["manifest"] == manifest == result
        assert not observed["snapshot"].exists()
        assert (archive / "database.dump").read_bytes() == b"unverified dump"


def test_change_while_snapshot_is_copied_fails_before_any_target_operation():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "archive"
        archive.mkdir()
        fixtures.LocalBackupTests.archive_fixture(archive)
        original_copy = local_backup.shutil.copyfile
        snapshots = []
        def copy(source, target, **kwargs):
            snapshots.append(Path(target).parent)
            Path(source).write_bytes(b"changed during snapshot")
            return original_copy(source, target, **kwargs)
        with patch.object(local_backup.shutil, "copyfile", side_effect=copy), \
             patch.object(local_backup, "_config") as config, \
             patch.object(local_backup, "_compose") as compose, \
             patch.object(local_backup, "_run") as run:
            with pytest.raises(local_backup.BackupError, match="checksum mismatch"):
                local_backup.restore_backup(archive, "synthetic-restored", 18712)
        assert not config.called and not compose.called and not run.called
        assert snapshots and all(not path.exists() for path in snapshots)


def test_failed_restore_releases_snapshot_and_preserves_source_archive():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "archive"
        archive.mkdir()
        manifest = fixtures.LocalBackupTests.archive_fixture(archive)
        paths = []
        original_copy = local_backup.shutil.copyfile
        def copy(source, target, **kwargs):
            paths.append(Path(target).parent)
            return original_copy(source, target, **kwargs)
        with patch.object(local_backup.shutil, "copyfile", side_effect=copy), \
             patch.object(local_backup, "_config", return_value={}), \
             patch.object(local_backup, "_compose", side_effect=local_backup.BackupError("synthetic target failure")):
            with pytest.raises(local_backup.BackupError, match="target failure"):
                local_backup.restore_backup(archive, "synthetic-restored", 18712)
        assert paths and all(not path.exists() for path in paths)
        assert local_backup.verify_backup(archive) == manifest


def test_fixture_receipts_are_pinned_with_database_and_cleaned_after_restore():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "archive"
        original = json.dumps(RECEIPTS).encode()
        manifest = capture_fixture(archive, receipt_bytes=original)
        captured = []
        def config(*_args):
            (archive / local_backup.RECEIPT_PATH).write_bytes(b"{}")
            return {}
        def run(arguments, **_kwargs):
            if arguments[:2] == ["docker", "cp"] and arguments[-1].endswith(":/data/receipts.json"):
                source = Path(arguments[2])
                captured.append((source, source.read_bytes()))
            return b""
        with patch.object(local_backup, "_config", side_effect=config), \
             patch.object(local_backup, "_compose", return_value=b""), \
             patch.object(local_backup, "_run", side_effect=run), \
             patch.object(local_backup, "_sql", side_effect=("0", "synthetic-revision", "FIXTURE", '["fictional-application"]')), \
             patch.object(local_backup, "_volumes", return_value=[]), \
             patch.object(local_backup, "_container", side_effect=lambda _p, service, _e: "synthetic-" + service), \
             patch.object(local_backup, "_wait_ready"):
            assert local_backup.restore_backup(archive, "synthetic-restored", 18712) == manifest
        assert len(captured) == 1 and captured[0][1] == original
        assert not captured[0][0].exists()


def test_snapshot_disk_failure_is_sanitized_before_target_operations():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "archive"
        archive.mkdir()
        manifest = fixtures.LocalBackupTests.archive_fixture(archive)
        with patch.object(local_backup.shutil, "copyfile", side_effect=OSError("private path disk failure")), \
             patch.object(local_backup, "_compose") as compose:
            with pytest.raises(local_backup.BackupError, match="snapshot could not be prepared") as error:
                local_backup.restore_backup(archive, "synthetic-restored", 18712)
        assert "private path" not in str(error.value)
        assert not compose.called
        assert local_backup.verify_backup(archive) == manifest


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Native FIFO fixture requires POSIX")
def test_unlisted_special_archive_node_is_rejected_before_restore():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "archive"
        archive.mkdir()
        fixtures.LocalBackupTests.archive_fixture(archive)
        os.mkfifo(archive / "documents" / "unlisted-pipe")
        with patch.object(local_backup, "_compose") as compose:
            with pytest.raises(local_backup.BackupError, match="unsupported"):
                local_backup.restore_backup(archive, "synthetic-restored", 18712)
        assert not compose.called
