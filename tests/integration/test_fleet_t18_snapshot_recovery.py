"""Public backup/restore recovery stays on one durable profile snapshot."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from uuid import uuid4
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from apps.api.main import create_app
from apps.api.profile_restore import create_profile_restore_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services import data_export
from services.data_export import DataExportError, export_profile_backup
from services.documents.uploads import save_resume
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
from tests.integration.test_spine import PROFILE

POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")


class FleetT18SnapshotRecoveryTests(unittest.TestCase):
    def test_backup_rejects_upload_metadata_that_changes_during_file_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(f"sqlite:///{(root / 'source.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            output = root / "output"
            output.mkdir()
            upload = save_resume(
                store, output, "fixture", "synthetic.txt", "text/plain",
                b"Synthetic resume bytes",
            )

            from services.documents import uploads

            real_read_resume = uploads.read_resume

            def return_changed_metadata(*args, **kwargs):
                metadata, contents = real_read_resume(*args, **kwargs)
                metadata["size_bytes"] += 1
                return metadata, contents

            try:
                with patch.object(uploads, "read_resume", side_effect=return_changed_metadata):
                    with self.assertRaises(DataExportError) as failure:
                        export_profile_backup(store, "fixture", output_dir=output)
                self.assertEqual(failure.exception.code, "EXPORT_FILE_UNAVAILABLE")
                self.assertEqual(upload["size_bytes"], len(b"Synthetic resume bytes"))
            finally:
                store.engine.dispose()

    def test_public_backup_is_consistent_during_concurrent_profile_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_url = f"sqlite:///{(root / 'source.db').as_posix()}"
            source = Store(source_url)
            source.migrate()
            source.bind_workspace_mode("FIXTURE")
            source.save_profile(copy.deepcopy(PROFILE))
            with source.engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            target = Store(f"sqlite:///{(root / 'restored.db').as_posix()}")
            target.migrate()
            target.bind_workspace_mode("FIXTURE")
            try:
                self._exercise_public_snapshot_recovery(source, target, root)
            finally:
                source.engine.dispose()
                target.engine.dispose()

    @unittest.skipUnless(POSTGRES_URL, "Requires disposable loopback MJOBS_TEST_POSTGRES_URL")
    def test_postgres_public_backup_is_consistent_during_concurrent_profile_update(self):
        base_url = _disposable_postgres_url(POSTGRES_URL)
        schema = "mjobs_t18_snapshot_" + uuid4().hex
        admin = create_engine(base_url, poolclass=NullPool, connect_args={"connect_timeout": 5})
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        try:
            url = base_url.update_query_dict({"options": "-csearch_path=" + schema
                + " -clock_timeout=5000 -cstatement_timeout=30000 -capplication_name=" + schema
            }).render_as_string(hide_password=False)
            from packages.persistence import resume_workspace  # noqa: F401

            source = Store(url)
            m.metadata.create_all(source.engine)
            source.bind_workspace_mode("FIXTURE")
            source.save_profile(copy.deepcopy(PROFILE))
            target = Store(url)
            with tempfile.TemporaryDirectory(prefix="mjobs-t18-snapshot-pg-") as temp:
                try:
                    self._exercise_public_snapshot_recovery(source, target, Path(temp))
                finally:
                    source.engine.dispose()
                    target.engine.dispose()
        finally:
            with admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
            admin.dispose()

    def _exercise_public_snapshot_recovery(self, source, target, root):
        output = root / "source-output"
        output.mkdir()
        app = create_app(source, "http://127.0.0.1:8090", output, operation_mode="FIXTURE")
        target.bind_workspace_mode("FIXTURE")
        restore_app = FastAPI()
        restore_app.include_router(create_profile_restore_router(target, root / "restore-output"))

        entered_versions_read = Event()
        resume_export = Event()
        real_profile_rows = data_export._profile_rows

        def pause_after_materialized_versions(connection, table, profile_id, columns):
            rows = real_profile_rows(connection, table, profile_id, columns)
            if table is m.profile_versions:
                entered_versions_read.set()
                if not resume_export.wait(timeout=10):
                    raise TimeoutError("test did not resume the paused backup export")
            return rows

        old_profile = source.profile("fixture")
        old_version = old_profile["version"]
        update_payload = copy.deepcopy(PROFILE)
        update_payload["facts"][0]["text"] = "Updated synthetic Python experience"
        update_payload["expected_version"] = old_version
        request_id = "t18-consistent-snapshot-" + uuid4().hex

        try:
            with patch.object(data_export, "_profile_rows", side_effect=pause_after_materialized_versions):
                with (TestClient(app) as client, TestClient(restore_app) as restore_client,
                      ThreadPoolExecutor(max_workers=1) as pool):
                    export_future = pool.submit(
                        lambda: client.get("/profiles/fixture/exports/backup.json"),
                    )
                    self.assertTrue(
                        entered_versions_read.wait(timeout=10),
                        "backup did not reach the materialized profile_versions seam",
                    )

                    updated = client.put("/profiles", json=update_payload)
                    self.assertEqual(updated.status_code, 200, updated.text)
                    self.assertNotEqual(updated.json()["version"], old_version)

                    resume_export.set()
                    backup_response = export_future.result(timeout=10)

                    self.assertEqual(backup_response.status_code, 200, backup_response.text)
                    backup = backup_response.json()
                    self.assertEqual(backup["profile"]["version"], old_version)
                    old_version_rows = [
                        row for row in backup["profile_versions"]
                        if row["version"] == old_version
                    ]
                    self.assertEqual(len(old_version_rows), 1)
                    old_facts = [row for row in backup["facts"] if row["version"] == old_version]
                    self.assertEqual(len(old_facts), len(PROFILE["facts"]))
                    self.assertEqual(
                        {row["text"] for row in old_facts},
                        {row["text"] for row in PROFILE["facts"]},
                    )

                    preview = restore_client.post("/profile-restores/preview", json={"backup": backup})
                    self.assertEqual(preview.status_code, 200, preview.text)
                    restored = restore_client.post("/profile-restores", json={
                        "backup": backup, "request_id": request_id,
                        "expected_fingerprint": preview.json()["fingerprint"], "confirmed": True,
                    })
                    self.assertEqual(restored.status_code, 201, restored.text)

            restored_id = restored.json()["profile_id"]
            self.assertEqual(target.profile(restored_id)["name"], PROFILE["name"])
            reopened = Store(target.url)
            try:
                persisted = reopened.profile(restored_id)
                self.assertEqual(persisted["name"], PROFILE["name"])
                self.assertEqual(
                    {fact["text"] for fact in persisted["facts"]},
                    {fact["text"] for fact in PROFILE["facts"]},
                )
            finally:
                reopened.engine.dispose()
        finally:
            resume_export.set()


if __name__ == "__main__":
    unittest.main()
