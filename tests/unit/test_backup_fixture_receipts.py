"""Fixture backups preserve independently persisted provider confirmations."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from cli import local_backup
from tests.unit.test_backup_config_snapshot import SAFE_SETTINGS


RECEIPTS = {"fictional-application": {
    "application_id": "fictional-application", "requisition": "A",
    "receipt": "MOCK-fictional", "timestamp": "2026-10-12T00:00:00Z",
    "confirmation_url": "http://mock:8090/ats/submit/A",
    "confirmation_text": "Fictional application received",
    "reference_number": "MOCK-fictional", "submission_count": 1,
}}


def capture_fixture(archive, *, receipt_bytes=None, mock_state="exited", history="1"):
    states = {service: "exited" for service in local_backup.WRITERS}
    states.update(postgres="running", mock=mock_state)
    settings = {**SAFE_SETTINGS, "MJOBS_OPERATION_MODE": "FIXTURE", "MJOBS_COMPOSE_MOCK": "1"}

    def compose(_project, *_arguments, stdout=None, **_kwargs):
        if stdout is not None:
            stdout.write(b"synthetic-database-dump")
        return b""

    def run(command, **_kwargs):
        destination = Path(command[-1])
        if ":/data/documents/" in command[-2]:
            (destination / "synthetic.pdf").write_bytes(b"synthetic-document")
        elif receipt_bytes is not None:
            # Docker initializes the /data volume with the image's empty
            # /data/documents directory, even for the mock service.
            (destination / "documents").mkdir()
            (destination / "receipts.json").write_bytes(receipt_bytes)
        return b""

    with patch.object(local_backup, "_config", return_value={}), \
         patch.object(local_backup, "_service_states", return_value=states), \
         patch.object(local_backup, "_container", side_effect=lambda _p, service, _e: "synthetic-" + service), \
         patch.object(local_backup, "_sql", side_effect=("FIXTURE", "synthetic-revision",
                                                       '[]' if history == '0' else '["fictional-application"]')), \
         patch.object(local_backup, "_compose", side_effect=compose), \
         patch.object(local_backup, "_run", side_effect=run), \
         patch.object(local_backup, "_runtime_configuration_snapshot", return_value={
             "schema_version": 1, "operation_mode": "FIXTURE", "settings": settings,
         }):
        return local_backup.create_backup("synthetic-source", archive)


def test_actual_backup_captures_exact_fixture_provider_receipts():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        original = json.dumps(RECEIPTS).encode()
        manifest = capture_fixture(archive, receipt_bytes=original)
        assert (archive / "mock" / "receipts.json").read_bytes() == original
        assert manifest["schema_version"] == 3
        assert manifest["files"]["mock/receipts.json"] == local_backup.checksum(archive / "mock/receipts.json")
        assert local_backup.verify_backup(archive) == manifest


@pytest.mark.parametrize("mock_state", ["running", "restarting", "missing"])
def test_backup_requires_quiescent_fixture_provider(mock_state):
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        with pytest.raises(local_backup.BackupError, match="mock"):
            capture_fixture(archive, receipt_bytes=json.dumps(RECEIPTS).encode(), mock_state=mock_state)
        assert not archive.exists()


def test_missing_receipts_cannot_silently_replace_confirmed_history():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        with pytest.raises(local_backup.BackupError, match="receipt"):
            capture_fixture(archive)
        assert not archive.exists()


def test_empty_new_fixture_workspace_remains_backupable():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        capture_fixture(archive, history="0")
        assert json.loads((archive / "mock/receipts.json").read_text()) == {}


@pytest.mark.parametrize("value", [[], {"wrong-id": RECEIPTS["fictional-application"]},
                                    {"fictional-application": {**RECEIPTS["fictional-application"], "submission_count": 2}}])
def test_malformed_provider_receipts_fail_without_publishing_archive(value):
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        with pytest.raises(local_backup.BackupError, match="receipt"):
            capture_fixture(archive, receipt_bytes=json.dumps(value).encode())
        assert not archive.exists()


def test_empty_receipts_cannot_replace_confirmed_history():
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(local_backup.BackupError, match="missing application history"):
            capture_fixture(Path(directory) / "backup", receipt_bytes=b"{}")


def restore_fixture(archive, manifest, *, required='["fictional-application"]'):
    calls = []

    def compose(_project, *arguments, **_kwargs):
        calls.append(("compose", arguments))
        return b""

    def run(arguments, **_kwargs):
        calls.append(("run", arguments))
        return b""

    with patch.object(local_backup, "verify_backup", return_value=manifest), \
         patch.object(local_backup, "_config", return_value={}), \
         patch.object(local_backup, "_compose", side_effect=compose), \
         patch.object(local_backup, "_run", side_effect=run), \
         patch.object(local_backup, "_sql", side_effect=("0", "synthetic-revision", "FIXTURE", required)), \
         patch.object(local_backup, "_volumes", return_value=[]), \
         patch.object(local_backup, "_container", side_effect=lambda _p, service, _e: "synthetic-" + service), \
         patch.object(local_backup, "_wait_ready"):
        local_backup.restore_backup(archive, "synthetic-restored", 18712)
    return calls


def test_fresh_restore_copies_provider_receipts_before_starting_application():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        manifest = capture_fixture(archive, receipt_bytes=json.dumps(RECEIPTS).encode())
        calls = restore_fixture(archive, manifest)
        copy = next(index for index, (_, args) in enumerate(calls) if "synthetic-mock:/data/receipts.json" in args)
        start = next(index for index, (_, args) in enumerate(calls) if "--no-build" in args)
        assert copy < start
        assert any("/data/receipts.json" in args and "chown" in args for _, args in calls)


def test_legacy_fixture_without_required_receipts_stops_before_workers_start():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        manifest = capture_fixture(archive, receipt_bytes=json.dumps(RECEIPTS).encode())
        manifest["schema_version"] = 2
        with pytest.raises(local_backup.BackupError, match="lacks fixture receipt history"):
            restore_fixture(archive, manifest)


def test_personal_archive_cannot_import_fixture_receipts():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        manifest = capture_fixture(archive, receipt_bytes=json.dumps(RECEIPTS).encode())
        manifest["operation_mode"] = "PERSONAL"
        (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(local_backup.BackupError, match="FIXTURE schema 3"):
            local_backup.verify_backup(archive)


def test_receipt_corruption_is_rejected_before_restore():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        capture_fixture(archive, receipt_bytes=json.dumps(RECEIPTS).encode())
        (archive / "mock/receipts.json").write_text("{}", encoding="utf-8")
        with pytest.raises(local_backup.BackupError, match="checksum mismatch"):
            local_backup.verify_backup(archive)


def test_oversize_receipts_are_rejected_before_json_decode():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "backup"
        with pytest.raises(local_backup.BackupError, match="size limit"):
            capture_fixture(archive, receipt_bytes=b" " * (local_backup.MAX_RECEIPT_SNAPSHOT_BYTES + 1))
        assert not archive.exists()
