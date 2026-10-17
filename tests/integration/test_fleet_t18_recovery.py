"""Team 18 connected durable export and interrupted restore recovery."""
import copy
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.data_export import create_data_export_router
from apps.api.main import create_app
from apps.api.profile_restore import create_profile_restore_router
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class FleetT18RecoveryTests(unittest.TestCase):
    def test_concurrent_public_profile_create_rejects_stale_creator_and_preserves_winner(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-fleet-t18-race-") as directory:
            database_url = f"sqlite:///{(Path(directory) / 'race.db').as_posix()}"
            store = Store(database_url)
            competing_store = Store(database_url)
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                request_profile = copy.deepcopy(PROFILE)
                request_profile.update(id="t18-race", name="Delayed Creator", email="delayed@example.test")
                winner_profile = copy.deepcopy(PROFILE)
                winner_profile.update(id="t18-race", name="Committed Winner", email="winner@example.test")

                delayed_at_insert = threading.Event()
                release_delayed = threading.Event()
                original_begin = store.engine.begin
                original_save = store.save_profile
                request_save = threading.local()

                def gate_delayed_creator():
                    if getattr(request_save, "active", False):
                        request_save.active = False
                        delayed_at_insert.set()
                        if not release_delayed.wait(timeout=5):
                            raise TimeoutError("timed out waiting for the competing creator")
                    return original_begin()

                def mark_api_save(*args, **kwargs):
                    request_save.active = True
                    return original_save(*args, **kwargs)

                app = create_app(store, "http://127.0.0.1:8090", Path(directory),
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                with TestClient(app) as client:
                    response_holder = []

                    def create_from_api():
                        response_holder.append(client.put("/profiles", json=request_profile))

                    with (patch.object(store.engine, "begin", side_effect=gate_delayed_creator),
                          patch.object(store, "save_profile", side_effect=mark_api_save)):
                        worker = threading.Thread(target=create_from_api, name="t18-delayed-api-create")
                        worker.start()
                        self.assertTrue(delayed_at_insert.wait(timeout=5), "API create did not reach insert")
                        winner_version = competing_store.save_profile(winner_profile)
                        release_delayed.set()
                        worker.join(timeout=5)
                        self.assertFalse(worker.is_alive(), "delayed API request remained blocked")

                    self.assertEqual(len(response_holder), 1)
                    self.assertEqual(response_holder[0].status_code, 409, response_holder[0].text)
                    actual = client.get("/profiles/t18-race")
                    self.assertEqual(actual.status_code, 200, actual.text)
                    self.assertEqual(actual.json()["name"], "Committed Winner")
                    self.assertEqual(actual.json()["email"], "winner@example.test")
                    self.assertEqual(actual.json()["version"], winner_version)
            finally:
                release_delayed.set()
                competing_store.engine.dispose()
                store.engine.dispose()

    def test_public_backup_survives_interrupted_restore_retry_and_database_reopen(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t18-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = Store(f"sqlite:///{(root / 'source.db').as_posix()}")
        self.addCleanup(source.engine.dispose)
        source.migrate()
        source.bind_workspace_mode("FIXTURE")
        profile = copy.deepcopy(PROFILE)
        profile.update(id="t18-source", name="Synthetic Recovery", email="recovery@example.test")
        source.save_profile(profile)
        with fixture_server() as origin:
            discover(source, origin, "t18-source")
        job = next(row for row in source.jobs("t18-source") if row["requisition"] == "B")
        application_id = prepare(source, job["id"], "t18-source", root)

        export_app = FastAPI()
        export_app.include_router(create_data_export_router(source, output_dir=root))
        with TestClient(export_app) as export_client:
            exported = export_client.get("/profiles/t18-source/exports/backup.json")
        self.assertEqual(exported.status_code, 200, exported.text)
        backup = exported.json()
        self.assertIn(application_id, {row["id"] for row in backup["applications"]})
        self.assertTrue(backup["document_files"])

        target_url = f"sqlite:///{(root / 'target.db').as_posix()}"
        target = Store(target_url)
        self.addCleanup(target.engine.dispose)
        target.migrate()
        target.bind_workspace_mode("FIXTURE")
        restore_root = root / "restored-files"
        restore_root.mkdir()
        target_app = FastAPI()
        target_app.include_router(create_profile_restore_router(target, restore_root))
        with TestClient(target_app) as restore_client:
            malformed = copy.deepcopy(backup)
            malformed.pop("profile_id")
            rejected = restore_client.post("/profile-restores/preview", json={"backup": malformed})
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(target.profiles(), [])

            preview = restore_client.post("/profile-restores/preview", json={"backup": backup})
            self.assertEqual(preview.status_code, 200, preview.text)
            request = {
                "backup": backup,
                "request_id": "t18-interrupted-then-retried",
                "expected_fingerprint": preview.json()["fingerprint"],
                "confirmed": True,
            }

            def interrupt_link(source_path, target_path):
                raise OSError("synthetic restore interruption")

            with patch("packages.persistence.profile_restore.os.link", side_effect=interrupt_link):
                interrupted = restore_client.post("/profile-restores", json=request)
            self.assertEqual(interrupted.status_code, 500, interrupted.text)
            self.assertEqual(interrupted.json()["detail"]["code"], "PROFILE_RESTORE_FILE_WRITE_FAILED")
            self.assertEqual(target.profiles(), [])

            restored = restore_client.post("/profile-restores", json=request)
            self.assertEqual(restored.status_code, 201, restored.text)
            restored_profile_id = restored.json()["profile_id"]

        target.engine.dispose()
        reopened = Store(target_url)
        try:
            restored_profile = reopened.profile(restored_profile_id)
            self.assertEqual(restored_profile["email"], "recovery@example.test")
            restored_jobs = reopened.jobs(restored_profile_id)
            self.assertTrue(restored_jobs)
            restored_applications = reopened.applications(restored_profile_id)
            self.assertEqual(len(restored_applications), 1)
            self.assertEqual(restored_applications[0]["job_id"], restored_jobs[0]["id"])
            with reopened.engine.connect() as connection:
                from packages.persistence import models as m

                records = connection.execute(m.documents.select().where(
                    m.documents.c.application_id == restored_applications[0]["id"]
                )).mappings().all()
            self.assertTrue(records)
            expected_hashes = sorted(record["sha256"] for record in backup["document_files"])
            restored_hashes = []
            self.assertTrue(all(row["files"] for row in records))
            for record in records:
                for file_format, filename in record["files"].items():
                    content = (restore_root / restored_applications[0]["id"] / filename).read_bytes()
                    self.assertEqual(len(content), next(
                        item["size_bytes"] for item in backup["document_files"]
                        if item["format"] == file_format and item["sha256"] == hashlib.sha256(content).hexdigest()
                    ))
                    restored_hashes.append(hashlib.sha256(content).hexdigest())
            self.assertEqual(sorted(restored_hashes), expected_hashes)
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
