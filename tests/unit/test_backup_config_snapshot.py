"""Bounded non-secret configuration snapshots are preserved and validated."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli import local_backup


SAFE_SETTINGS = {
    "MJOBS_OPERATION_MODE": "PERSONAL",
    "MJOBS_COMPOSE_MOCK": "0",
    "MJOBS_SOURCE_COLLECTION_CONCURRENCY": "2",
    "MJOBS_COMPANY_DISCOVERY_CONCURRENCY": "1",
    "MJOBS_AI_CONCURRENCY": "1",
    "MJOBS_DOCUMENT_GENERATION_CONCURRENCY": "1",
    "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "1",
    "MJOBS_TRANSIENT_RETENTION_INTERVAL_SECONDS": "86400",
}
PRIVATE_SENTINEL = "synthetic-never-archive-secret"
SUBMISSION_SETTINGS = {
    "MJOBS_SUBMISSION_HOURLY_LIMIT": "0",
    "MJOBS_SUBMISSION_DAILY_LIMIT": "12",
    "MJOBS_SUBMISSION_COMPANY_LIMIT": "2",
}


class BackupConfigurationSnapshotTests(unittest.TestCase):
    def test_runtime_v2_records_submission_caps_without_private_values(self):
        settings = {**SAFE_SETTINGS, **SUBMISSION_SETTINGS}
        raw_environment = [f"{name}={value}" for name, value in settings.items()]
        raw_environment.append("MJOBS_CREDENTIAL_ENCRYPTION_KEY=" + PRIVATE_SENTINEL)
        with patch.object(local_backup, "_run", return_value=json.dumps(raw_environment).encode()):
            snapshot = local_backup._runtime_configuration_snapshot("synthetic-api-container", "PERSONAL")
        self.assertEqual(snapshot, {
            "schema_version": 2, "operation_mode": "PERSONAL", "settings": settings})
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(snapshot))

    def test_runtime_v2_requires_all_caps_and_rejects_invalid_or_secret_fields(self):
        valid = {**SAFE_SETTINGS, **SUBMISSION_SETTINGS}
        variants = [
            {key: value for key, value in valid.items() if key != "MJOBS_SUBMISSION_DAILY_LIMIT"},
            {**valid, "MJOBS_CREDENTIAL_ENCRYPTION_KEY": PRIVATE_SENTINEL},
            *({**valid, "MJOBS_SUBMISSION_HOURLY_LIMIT": value}
              for value in ("", "-1", "10001", "1.5", PRIVATE_SENTINEL, None, True)),
        ]
        for index, settings in enumerate(variants):
            with self.subTest(case=index), self.assertRaises(local_backup.BackupError) as failure:
                local_backup._validate_runtime_configuration({
                    "schema_version": 2, "operation_mode": "PERSONAL", "settings": settings}, "PERSONAL")
            self.assertNotIn(PRIVATE_SENTINEL, str(failure.exception))
        with self.assertRaises(local_backup.BackupError):
            local_backup._validate_runtime_configuration({
                "schema_version": 1, "operation_mode": "PERSONAL", "settings": valid}, "PERSONAL")

    def test_partial_caps_in_runtime_environment_fail_instead_of_defaulting_a_snapshot(self):
        settings = {**SAFE_SETTINGS, "MJOBS_SUBMISSION_HOURLY_LIMIT": "0"}
        with patch.object(local_backup, "_run", return_value=json.dumps(
                [f"{name}={value}" for name, value in settings.items()]).encode()):
            with self.assertRaises(local_backup.BackupError):
                local_backup._runtime_configuration_snapshot("synthetic-api-container", "PERSONAL")

    def test_runtime_projection_keeps_only_bounded_allowlisted_api_settings(self):
        raw_environment = [f"{name}={value}" for name, value in SAFE_SETTINGS.items()]
        raw_environment.extend((
            "MJOBS_DATABASE_URL=postgresql://mjobs:" + PRIVATE_SENTINEL + "@postgres/mjobs",
            "MJOBS_REDIS_URL=redis://private-redis:6379/0",
            "MJOBS_CREDENTIAL_ENCRYPTION_KEY=" + PRIVATE_SENTINEL,
            "MJOBS_TRANSIENT_RETENTION_CONFIG=" + PRIVATE_SENTINEL,
            "UNRELATED_PRIVATE_VALUE=" + PRIVATE_SENTINEL,
        ))
        with patch.object(local_backup, "_run", return_value=json.dumps(raw_environment).encode("utf-8")) as run:
            snapshot = local_backup._runtime_configuration_snapshot("synthetic-api-container", "PERSONAL")

        self.assertEqual(snapshot, {
            "schema_version": 1,
            "operation_mode": "PERSONAL",
            "settings": SAFE_SETTINGS,
        })
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(snapshot))
        self.assertNotIn("DATABASE_URL", json.dumps(snapshot))
        self.assertEqual(run.call_args.args[0][-1], "synthetic-api-container")

    def test_runtime_projection_rejects_invalid_allowlisted_values_without_echoing_them(self):
        invalid = dict(SAFE_SETTINGS)
        invalid["MJOBS_SOURCE_COLLECTION_CONCURRENCY"] = "5"
        with patch.object(local_backup, "_run", return_value=json.dumps(
                [f"{name}={value}" for name, value in invalid.items()]).encode("utf-8")):
            with self.assertRaises(local_backup.BackupError) as error:
                local_backup._runtime_configuration_snapshot("synthetic-api-container", "PERSONAL")
        self.assertNotIn("5", str(error.exception))

    def test_schema_two_archive_preserves_and_checks_known_configuration_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)
            (archive / "documents").mkdir()
            (archive / "configuration").mkdir()
            compose = (local_backup.ROOT / "compose.yml").read_bytes()
            (archive / "configuration" / "compose.yml").write_bytes(compose)
            (archive / "database.dump").write_bytes(b"synthetic-dump")
            runtime = {
                "schema_version": 1,
                "operation_mode": "PERSONAL",
                "settings": SAFE_SETTINGS,
            }
            runtime_path = archive / "configuration" / "runtime.json"
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            files = {
                name: local_backup.checksum(archive.joinpath(*name.split("/")))
                for name in ("database.dump", "configuration/compose.yml", "configuration/runtime.json")
            }
            manifest = {
                "schema_version": 2,
                "source_project": "synthetic-source",
                "operation_mode": "PERSONAL",
                "alembic_revision": "synthetic-revision",
                "compose_sha256": hashlib.sha256(compose).hexdigest(),
                "files": files,
            }
            (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            self.assertEqual(local_backup.verify_backup(archive), manifest)

            # New readers preserve legacy archives and verify the new runtime
            # projection through the complete, checksummed archive boundary.
            runtime.update(schema_version=2, settings={**SAFE_SETTINGS, **SUBMISSION_SETTINGS})
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            files["configuration/runtime.json"] = local_backup.checksum(runtime_path)
            (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(local_backup.verify_backup(archive), manifest)

    def test_schema_two_rejects_secret_fields_and_unknown_configuration_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)
            (archive / "documents").mkdir()
            (archive / "configuration").mkdir()
            compose = (local_backup.ROOT / "compose.yml").read_bytes()
            compose_path = archive / "configuration" / "compose.yml"
            compose_path.write_bytes(compose)
            (archive / "database.dump").write_bytes(b"synthetic-dump")
            runtime = {
                "schema_version": 1,
                "operation_mode": "PERSONAL",
                "settings": {**SAFE_SETTINGS, "MJOBS_CREDENTIAL_ENCRYPTION_KEY": PRIVATE_SENTINEL},
            }
            runtime_path = archive / "configuration" / "runtime.json"
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            files = {
                name: local_backup.checksum(archive.joinpath(*name.split("/")))
                for name in ("database.dump", "configuration/compose.yml", "configuration/runtime.json")
            }
            manifest = {
                "schema_version": 2,
                "source_project": "synthetic-source",
                "operation_mode": "PERSONAL",
                "alembic_revision": "synthetic-revision",
                "compose_sha256": hashlib.sha256(compose).hexdigest(),
                "files": files,
            }
            (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(local_backup.BackupError) as error:
                local_backup.verify_backup(archive)
            self.assertNotIn(PRIVATE_SENTINEL, str(error.exception))

            runtime["settings"] = SAFE_SETTINGS
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            files["configuration/runtime.json"] = local_backup.checksum(runtime_path)
            (archive / "unexpected.txt").write_text("not allowed", encoding="utf-8")
            (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(local_backup.BackupError, "unlisted"):
                local_backup.verify_backup(archive)

            (archive / "unexpected.txt").unlink()
            (archive / "configuration" / "nested").mkdir()
            with self.assertRaisesRegex(local_backup.BackupError, "unsupported path"):
                local_backup.verify_backup(archive)

            (archive / "configuration" / "nested").rmdir()
            compose_path.write_bytes(b"x" * (local_backup.MAX_COMPOSE_SNAPSHOT_BYTES + 1))
            files["configuration/compose.yml"] = local_backup.checksum(compose_path)
            manifest["compose_sha256"] = files["configuration/compose.yml"]
            (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(local_backup.BackupError, "size limit"):
                local_backup.verify_backup(archive)

    def test_backup_writes_only_the_validated_runtime_projection_and_public_compose_source(self):
        states = {service: "exited" for service in local_backup.WRITERS}
        states["postgres"] = "running"

        def fake_compose(_project, *_arguments, stdout=None, **_kwargs):
            stdout.write(b"synthetic-database-dump")
            return b""

        def fake_run(command, **_kwargs):
            destination = Path(command[-1])
            (destination / "synthetic.pdf").write_bytes(b"synthetic-document")
            return b""

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "backup"
            with patch.object(local_backup, "_config", return_value={}), \
                 patch.object(local_backup, "_service_states", return_value=states), \
                 patch.object(local_backup, "_container", return_value="synthetic-api-container"), \
                 patch.object(local_backup, "_sql", side_effect=("PERSONAL", "synthetic-revision")), \
                 patch.object(local_backup, "_compose", side_effect=fake_compose), \
                 patch.object(local_backup, "_run", side_effect=fake_run), \
                 patch.object(local_backup, "_runtime_configuration_snapshot", return_value={
                     "schema_version": 1,
                     "operation_mode": "PERSONAL",
                     "settings": SAFE_SETTINGS,
                 }):
                manifest = local_backup.create_backup("synthetic-source", archive)

            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual((archive / "configuration" / "compose.yml").read_bytes(),
                             (local_backup.ROOT / "compose.yml").read_bytes())
            runtime_bytes = (archive / "configuration" / "runtime.json").read_bytes()
            self.assertEqual(json.loads(runtime_bytes)["settings"], SAFE_SETTINGS)
            self.assertNotIn(PRIVATE_SENTINEL.encode(), runtime_bytes)
            self.assertEqual(local_backup.verify_backup(archive), manifest)


if __name__ == "__main__":
    unittest.main()
