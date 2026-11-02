"""Bounded non-secret configuration snapshots are preserved and validated."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cli import local_backup
from services.applications.browser_profiles import browser_profile_path


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
PROFILE_NOTIFICATION_LOG_SETTINGS = {
    "MJOBS_BROWSER_PROFILE": "job-search",
    "MJOBS_NOTIFICATIONS_ENABLED": "1",
    "MJOBS_WORKER_LOG_LEVEL": " warning ",
}


class BackupConfigurationSnapshotTests(unittest.TestCase):
    def test_runtime_v3_records_validated_nonsecret_configuration_without_private_values(self):
        for mode in ("PERSONAL", "FIXTURE"):
            settings = {**SAFE_SETTINGS, **SUBMISSION_SETTINGS, **PROFILE_NOTIFICATION_LOG_SETTINGS,
                        "MJOBS_OPERATION_MODE": mode,
                        "MJOBS_COMPOSE_MOCK": "1" if mode == "FIXTURE" else "0"}
            raw_environment = [f"{name}={value}" for name, value in settings.items()]
            raw_environment.extend((
                "MJOBS_CREDENTIAL_ENCRYPTION_KEY=" + PRIVATE_SENTINEL,
                "MJOBS_DATABASE_URL=" + PRIVATE_SENTINEL,
                "MJOBS_TRANSIENT_RETENTION_CONFIG=" + PRIVATE_SENTINEL,
            ))
            with self.subTest(mode=mode), patch.object(
                    local_backup, "_run", return_value=json.dumps(raw_environment).encode()):
                snapshot = local_backup._runtime_configuration_snapshot("synthetic-api-container", mode)
            self.assertEqual(snapshot, {
                "schema_version": 3, "operation_mode": mode, "settings": settings})
            self.assertNotIn(PRIVATE_SENTINEL, json.dumps(snapshot))

    def test_old_container_with_logging_and_notifications_still_captures_legacy_version(self):
        # The guarded updater backs up the OLD container before switching to a
        # release containing the browser selector. Existing optional variables
        # must not make that pre-upgrade archive demand the new selector.
        for version, settings in ((1, SAFE_SETTINGS), (2, {**SAFE_SETTINGS, **SUBMISSION_SETTINGS})):
            environment = {**settings, "MJOBS_WORKER_LOG_LEVEL": "WARNING",
                           "MJOBS_NOTIFICATIONS_ENABLED": "0"}
            with self.subTest(version=version), patch.object(local_backup, "_run", return_value=json.dumps(
                    [f"{name}={value}" for name, value in environment.items()]).encode()):
                snapshot = local_backup._runtime_configuration_snapshot("synthetic-api-container", "PERSONAL")
            self.assertEqual(snapshot, {
                "schema_version": version, "operation_mode": "PERSONAL", "settings": settings})

    def test_runtime_v3_refuses_missing_invalid_or_private_configuration_without_echoing_it(self):
        valid = {**SAFE_SETTINGS, **SUBMISSION_SETTINGS, **PROFILE_NOTIFICATION_LOG_SETTINGS}
        variants = [
            *({key: value for key, value in valid.items() if key != missing}
              for missing in (*PROFILE_NOTIFICATION_LOG_SETTINGS, *SUBMISSION_SETTINGS)),
            {**valid, "MJOBS_CREDENTIAL_ENCRYPTION_KEY": PRIVATE_SENTINEL},
            *({**valid, "MJOBS_BROWSER_PROFILE": value}
              for value in ("../private", "C:\\private", "con", "com1", "", None, True, PRIVATE_SENTINEL + "/")),
            *({**valid, "MJOBS_NOTIFICATIONS_ENABLED": value}
              for value in ("true", " 1 ", "", None, True, 1, PRIVATE_SENTINEL)),
            *({**valid, "MJOBS_WORKER_LOG_LEVEL": value}
              for value in ("TRACE", "", "WARNING\nPRIVATE", None, True, 20, PRIVATE_SENTINEL)),
            {**valid, "MJOBS_SUBMISSION_DAILY_LIMIT": "-1"},
        ]
        for index, settings in enumerate(variants):
            with self.subTest(case=index), self.assertRaises(local_backup.BackupError) as failure:
                local_backup._validate_runtime_configuration({
                    "schema_version": 3, "operation_mode": "PERSONAL", "settings": settings}, "PERSONAL")
            self.assertNotIn(PRIVATE_SENTINEL, str(failure.exception))
        for missing in ("MJOBS_WORKER_LOG_LEVEL", "MJOBS_NOTIFICATIONS_ENABLED", "MJOBS_SUBMISSION_HOURLY_LIMIT"):
            environment = {key: value for key, value in valid.items() if key != missing}
            with self.subTest(capture_missing=missing), patch.object(local_backup, "_run", return_value=json.dumps(
                    [f"{name}={value}" for name, value in environment.items()]).encode()):
                with self.assertRaises(local_backup.BackupError):
                    local_backup._runtime_configuration_snapshot("synthetic-api-container", "PERSONAL")

    def test_runtime_v3_log_level_validation_matches_worker_startup(self):
        from workers.main import _WORKER_LOG_LEVELS, _worker_log_level

        self.assertEqual(local_backup.WORKER_LOG_LEVELS, _WORKER_LOG_LEVELS)
        for level in _WORKER_LOG_LEVELS:
            captured = f" {level.lower()} "
            settings = {**SAFE_SETTINGS, **SUBMISSION_SETTINGS, **PROFILE_NOTIFICATION_LOG_SETTINGS,
                        "MJOBS_WORKER_LOG_LEVEL": captured}
            with self.subTest(level=level), patch.dict(os.environ, {"MJOBS_WORKER_LOG_LEVEL": captured}):
                self.assertEqual(_worker_log_level(), level)
                local_backup._validate_runtime_configuration({
                    "schema_version": 3, "operation_mode": "PERSONAL", "settings": settings}, "PERSONAL")

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

            runtime.update(schema_version=3, settings={
                **SAFE_SETTINGS, **SUBMISSION_SETTINGS, **PROFILE_NOTIFICATION_LOG_SETTINGS})
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

    def test_backup_preserves_pinned_selection_but_excludes_all_named_and_legacy_cookies(self):
        states = {service: "exited" for service in local_backup.WRITERS}
        states["postgres"] = "running"
        settings = {**SAFE_SETTINGS, **SUBMISSION_SETTINGS, **PROFILE_NOTIFICATION_LOG_SETTINGS}

        def fake_compose(_project, *_arguments, stdout=None, **_kwargs):
            stdout.write(b"synthetic-database-dump")
            return b""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_documents = root / "source-documents"
            application = source_documents / ("a" * 64)
            with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "job-search"}):
                profile = browser_profile_path(application, initialize=True)
            cookie = profile / "Default" / "Network" / "Cookies"
            cookie.parent.mkdir(parents=True)
            cookie.write_text(PRIVATE_SENTINEL, encoding="utf-8")
            other_profile = application / "browser-profile" / "other-label"
            other_profile.mkdir()
            (other_profile / "Cookies").write_text(PRIVATE_SENTINEL, encoding="utf-8")
            legacy = source_documents / ("b" * 64) / "browser-profile"
            legacy.mkdir(parents=True)
            (legacy / "Cookies").write_text(PRIVATE_SENTINEL, encoding="utf-8")
            (application / "resume.pdf").write_bytes(b"synthetic-generated-document")
            selection = application / "browser-profile-selection.json"
            original_selection = selection.read_bytes()

            def fake_run(command, **_kwargs):
                if command[:2] == ["docker", "cp"]:
                    shutil.copytree(source_documents, Path(command[-1]), dirs_exist_ok=True)
                    return b""
                self.assertEqual(command[:2], ["docker", "inspect"])
                return json.dumps([f"{name}={value}" for name, value in settings.items()] + [
                    "MJOBS_CREDENTIAL_ENCRYPTION_KEY=" + PRIVATE_SENTINEL]).encode()

            archive = root / "backup"
            with patch.object(local_backup, "_config", return_value={}), \
                 patch.object(local_backup, "_service_states", return_value=states), \
                 patch.object(local_backup, "_container", return_value="synthetic-api-container"), \
                 patch.object(local_backup, "_sql", side_effect=("PERSONAL", "synthetic-revision")), \
                 patch.object(local_backup, "_compose", side_effect=fake_compose), \
                 patch.object(local_backup, "_run", side_effect=fake_run):
                manifest = local_backup.create_backup("synthetic-source", archive)

            self.assertEqual(manifest["schema_version"], 2, "Runtime v3 does not change the archive schema")
            self.assertEqual(local_backup.verify_backup(archive), manifest)
            runtime = json.loads((archive / "configuration" / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime, {"schema_version": 3, "operation_mode": "PERSONAL", "settings": settings})
            archived_application = archive / "documents" / application.name
            self.assertEqual((archived_application / selection.name).read_bytes(), original_selection)
            self.assertIn(f"documents/{application.name}/{selection.name}", manifest["files"])
            self.assertEqual((archived_application / "resume.pdf").read_bytes(), b"synthetic-generated-document")
            self.assertFalse(any(path.name == "browser-profile" for path in (archive / "documents").rglob("*")))
            self.assertFalse(any("browser-profile/" in name for name in manifest["files"]))
            self.assertTrue(all(PRIVATE_SENTINEL.encode() not in path.read_bytes()
                                for path in archive.rglob("*") if path.is_file()))
            self.assertEqual(cookie.read_text(encoding="utf-8"), PRIVATE_SENTINEL,
                             "Backup exclusion must not change the source session")

            # This is a fresh file-copy recovery fixture, not a live restore or
            # a reused cookie session. A changed default cannot redirect the
            # application's recovered, pinned selection.
            recovered_documents = root / "recovered-documents"
            shutil.copytree(archive / "documents", recovered_documents)
            with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "different-default"}):
                recovered = browser_profile_path(recovered_documents / application.name, initialize=True)
            self.assertEqual(recovered, recovered_documents / application.name / "browser-profile" / "job-search")
            self.assertTrue(recovered.is_dir())
            self.assertEqual(list(recovered.iterdir()), [])
            self.assertEqual((recovered_documents / application.name / selection.name).read_bytes(), original_selection)


if __name__ == "__main__":
    unittest.main()
