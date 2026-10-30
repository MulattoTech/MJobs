"""Prepare-time binding for an explicitly reviewed cover-letter revision.

Browser and adapter upload remain outside this precursor test surface.
"""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.engine import Connection

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.cover_letter_preview import cover_letter_preview_fingerprint
from services.documents.generate import prepare
from services.documents.reviewed_cover_letter import materialize_reviewed_preview
from services.applications.upload_integrity import snapshot_prepared
from tests.integration.test_spine import PROFILE


class ReviewedCoverLetterPrepareBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-reviewed-cover-letter-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "documents"
        self.store = Store(f"sqlite:///{(self.root / 'reviewed-cover-letter.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job = next(row for row in self.store.jobs("fixture")
                        if row["requisition"] == "B")
        self.application_id = prepare(
            self.store, self.job["id"], "fixture", self.output,
        )
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.output,
            operation_mode="FIXTURE",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _approve(self, *, request_id="reviewed-cover-letter-1", style="warm"):
        profile = self.store.profile("fixture")
        fingerprint = cover_letter_preview_fingerprint(self.job)
        preview = self.client.post(
            f"/profiles/fixture/jobs/{self.job['id']}/cover-letter-preview",
            json={
                "expected_profile_version": profile["version"],
                "expected_job_fingerprint": fingerprint,
                "style": style,
                "length": "concise",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_sha256 = hashlib.sha256(
            preview.json()["preview_text"].encode("utf-8")
        ).hexdigest()
        base = self.store.application(self.application_id)
        approved = self.client.post(
            f"/profiles/fixture/applications/{self.application_id}/cover-letter-reviews",
            json={
                "expected_profile_version": profile["version"],
                "expected_job_fingerprint": fingerprint,
                "expected_base_document_version": base["document_version"],
                "style": style,
                "length": "concise",
                "preview_sha256": preview_sha256,
                "request_id": request_id,
            },
        )
        return approved, base, profile

    def _reprepare(self, reviewed, base, profile):
        return self.client.post(
            f"/profiles/fixture/applications/{self.application_id}/reprepare",
            json={
                "expected_document_version": base["document_version"],
                "expected_application_profile_version": base["profile_version"],
                "expected_profile_version": profile["version"],
                "expected_state": base["state"],
                "reviewed_cover_letter_document_id": reviewed["document_id"],
                "reviewed_cover_letter_sha256": reviewed["preview_sha256"],
            },
        )

    def _crash_after_review_directory_publish(self):
        class SimulatedProcessCrash(BaseException):
            pass

        original_execute = Connection.execute

        def crash_on_document_insert(connection, statement, *args, **kwargs):
            if (getattr(statement, "is_insert", False)
                    and getattr(statement, "table", None) is m.documents):
                raise SimulatedProcessCrash("after directory publication")
            return original_execute(connection, statement, *args, **kwargs)

        with patch.object(Connection, "execute", new=crash_on_document_insert):
            with self.assertRaises(SimulatedProcessCrash):
                self._approve(request_id="reviewed-cover-letter-crash-window")

    def test_review_approval_materializes_one_future_revision_without_promoting_it(self):
        approved, base, _profile = self._approve()

        self.assertEqual(approved.status_code, 201, approved.text)
        result = approved.json()
        self.assertEqual(result["application_id"], self.application_id)
        self.assertEqual(result["base_document_version"], base["document_version"])
        self.assertEqual(result["version"], base["document_version"] + 1)
        self.assertRegex(result["preview_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         base["document_version"])
        with self.store.engine.connect() as connection:
            rows = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == base["document_version"] + 1,
                m.documents.c.kind == "cover-letter",
            )).mappings())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], result["document_id"])
        self.assertEqual(rows[0]["generation_context"]["review_status"], "APPROVED")

    def test_reprepare_promotes_exact_reviewed_revision_and_manifest_survives_reopen(self):
        approved, base, profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)
        reviewed = approved.json()

        response = self._reprepare(reviewed, base, profile)

        self.assertEqual(response.status_code, 200, response.text)
        promoted = response.json()
        self.assertEqual(promoted["document_version"], reviewed["version"])
        with self.store.engine.connect() as connection:
            current = [dict(row) for row in connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == reviewed["version"],
            )).mappings()]
            self.assertEqual(connection.execute(select(m.attempts.c.id)).all(), [])
        self.assertEqual({row["kind"] for row in current}, {"resume", "cover-letter"})
        letters = [row for row in current if row["kind"] == "cover-letter"]
        self.assertEqual(len(letters), 1)
        self.assertEqual(letters[0]["id"], reviewed["document_id"])
        self.assertEqual(letters[0]["generation_context"]["reviewed_preview_sha256"],
                         reviewed["preview_sha256"])
        pdf = self.output / self.application_id / letters[0]["files"]["pdf"]
        manifest = snapshot_prepared(letters[0], pdf).manifest()
        self.assertEqual(manifest["document_id"], reviewed["document_id"])
        self.assertEqual(manifest["sha256"], letters[0]["sha256"]["pdf"])
        self.assertEqual(promoted["state"], "PREPARED")
        self.assertIsNone(promoted["confirmation"])

        reopened = Store(str(self.store.engine.url))
        try:
            self.assertEqual(reopened.application(self.application_id)["document_version"],
                             reviewed["version"])
            with reopened.engine.connect() as connection:
                rows = list(connection.execute(select(m.documents).where(
                    m.documents.c.application_id == self.application_id,
                    m.documents.c.version == reviewed["version"],
                )).mappings())
            self.assertEqual({row["id"] for row in rows},
                             {row["id"] for row in current})
        finally:
            reopened.engine.dispose()

    def test_approval_replay_is_idempotent_but_competing_request_conflicts(self):
        approved, base, _profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)

        replay, _, _ = self._approve()
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["document_id"], approved.json()["document_id"])

        competing, _, _ = self._approve(
            request_id="reviewed-cover-letter-2", style="direct",
        )
        self.assertEqual(competing.status_code, 409, competing.text)
        self.assertEqual(competing.json()["detail"]["code"],
                         "COVER_LETTER_REVIEW_VERSION_CONFLICT")
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         base["document_version"])

    def test_approval_replay_rejects_each_deleted_saved_artifact(self):
        approved, _base, _profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)
        with self.store.engine.connect() as connection:
            saved = dict(connection.execute(select(m.documents).where(
                m.documents.c.id == approved.json()["document_id"],
            )).mappings().one())

        for extension in ("docx", "pdf", "txt"):
            with self.subTest(extension=extension):
                path = self.output / self.application_id / saved["files"][extension]
                original = path.read_bytes()
                path.unlink()
                try:
                    replay, _, _ = self._approve()
                    self.assertEqual(replay.status_code, 422, replay.text)
                    self.assertEqual(replay.json()["detail"]["code"],
                                     "COVER_LETTER_REVIEW_CONTENT_CHANGED")
                finally:
                    path.write_bytes(original)

    def test_approval_replay_rejects_each_modified_saved_artifact(self):
        approved, _base, _profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)
        with self.store.engine.connect() as connection:
            saved = dict(connection.execute(select(m.documents).where(
                m.documents.c.id == approved.json()["document_id"],
            )).mappings().one())

        for extension in ("docx", "pdf", "txt"):
            with self.subTest(extension=extension):
                path = self.output / self.application_id / saved["files"][extension]
                original = path.read_bytes()
                path.write_bytes(original + b"modified")
                try:
                    replay, _, _ = self._approve()
                    self.assertEqual(replay.status_code, 422, replay.text)
                    self.assertEqual(replay.json()["detail"]["code"],
                                     "COVER_LETTER_REVIEW_CONTENT_CHANGED")
                finally:
                    path.write_bytes(original)

    def test_approval_replay_rejects_saved_path_redirect_outside_application(self):
        approved, _base, _profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)
        document_id = approved.json()["document_id"]
        with self.store.engine.connect() as connection:
            saved = dict(connection.execute(select(m.documents).where(
                m.documents.c.id == document_id,
            )).mappings().one())
        redirected = {**saved["files"], "docx": "../redirected-cover-letter.docx"}
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(
                m.documents.c.id == document_id,
            ).values(files=redirected))

        replay, _, _ = self._approve()

        self.assertEqual(replay.status_code, 422, replay.text)
        self.assertEqual(replay.json()["detail"]["code"],
                         "COVER_LETTER_REVIEW_PATH_INVALID")

    def test_stale_and_foreign_approval_fail_before_materialization(self):
        profile = self.store.profile("fixture")
        foreign_profile = deepcopy(PROFILE)
        foreign_profile.update({"id": "foreign", "name": "Foreign fixture",
                                "email": "foreign@example.invalid"})
        self.store.save_profile(foreign_profile)
        fingerprint = cover_letter_preview_fingerprint(self.job)
        preview = self.client.post(
            f"/profiles/fixture/jobs/{self.job['id']}/cover-letter-preview",
            json={"expected_profile_version": profile["version"],
                  "expected_job_fingerprint": fingerprint},
        ).json()
        body = {
            "expected_profile_version": profile["version"],
            "expected_job_fingerprint": fingerprint,
            "expected_base_document_version": 999,
            "preview_sha256": hashlib.sha256(
                preview["preview_text"].encode("utf-8")
            ).hexdigest(),
            "request_id": "reviewed-cover-letter-stale",
        }
        stale = self.client.post(
            f"/profiles/fixture/applications/{self.application_id}/cover-letter-reviews",
            json=body,
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        foreign = self.client.post(
            f"/profiles/foreign/applications/{self.application_id}/cover-letter-reviews",
            json=body,
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

    def _assert_approval_drift_rejected(self, name, table, predicate, values):
        def drift_then_materialize(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(table).where(predicate).values(**values))
            return materialize_reviewed_preview(*args, **kwargs)

        with patch(
            "apps.api.cover_letter_preview.materialize_reviewed_preview",
            side_effect=drift_then_materialize,
        ):
            response, base, _profile = self._approve(
                request_id=f"reviewed-cover-letter-{name}-drift",
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"],
                         "COVER_LETTER_REVIEW_CONTEXT_STALE")
        with self.store.engine.connect() as connection:
            future = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == base["document_version"] + 1,
            )).mappings())
        self.assertEqual(future, [])

    def test_profile_drift_during_approval_fails_before_insert(self):
        self._assert_approval_drift_rejected(
            "profile", m.profiles, m.profiles.c.id == "fixture",
            {"version": "f" * 64},
        )

    def test_job_drift_during_approval_fails_before_insert(self):
        self._assert_approval_drift_rejected(
            "job", m.jobs, m.jobs.c.id == self.job["id"],
            {"title": "Changed during approval"},
        )

    def test_approval_uses_prepare_lock_order(self):
        locked_tables = []

        def observe_lock(_connection, clause, _multiparams, _params, _options):
            if getattr(clause, "_for_update_arg", None) is None:
                return
            statement = str(clause)
            for name in ("career_profiles", "canonical_jobs", "applications"):
                if name in statement:
                    locked_tables.append(name)
                    break

        event.listen(self.store.engine, "before_execute", observe_lock)
        try:
            approved, _base, _profile = self._approve()
        finally:
            event.remove(self.store.engine, "before_execute", observe_lock)
        self.assertEqual(approved.status_code, 201, approved.text)
        self.assertEqual(locked_tables[-3:], [
            "career_profiles", "canonical_jobs", "applications",
        ])

    def test_concurrent_approval_replay_and_reprepare_complete_without_deadlock(self):
        approved, base, profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)
        reviewed = approved.json()
        barrier = Barrier(2)
        approval_body = {
            "expected_profile_version": profile["version"],
            "expected_job_fingerprint": cover_letter_preview_fingerprint(self.job),
            "expected_base_document_version": base["document_version"],
            "style": "warm",
            "length": "concise",
            "preview_sha256": reviewed["preview_sha256"],
            "request_id": "reviewed-cover-letter-1",
        }

        def replay():
            barrier.wait(timeout=5)
            return self.client.post(
                f"/profiles/fixture/applications/{self.application_id}/cover-letter-reviews",
                json=approval_body,
            )

        def reprepare():
            barrier.wait(timeout=5)
            return self._reprepare(reviewed, base, profile)

        with ThreadPoolExecutor(max_workers=2) as executor:
            replay_future = executor.submit(replay)
            reprepare_future = executor.submit(reprepare)
            replay_response = replay_future.result(timeout=10)
            reprepare_response = reprepare_future.result(timeout=10)
        self.assertIn(replay_response.status_code, (201, 409), replay_response.text)
        self.assertEqual(reprepare_response.status_code, 200, reprepare_response.text)
        with self.store.engine.connect() as connection:
            promoted = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == reviewed["version"],
            )).mappings())
        self.assertEqual(sorted(row["kind"] for row in promoted),
                         ["cover-letter", "resume"])

    def test_unreviewed_foreign_or_changed_artifact_fails_closed(self):
        approved, base, profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)
        reviewed = approved.json()
        with self.store.engine.connect() as connection:
            original = dict(connection.execute(select(m.documents).where(
                m.documents.c.id == reviewed["document_id"],
            )).mappings().one())
        original_context = dict(original["generation_context"])
        wrong_application = self._reprepare(
            {**reviewed, "document_id": "0" * 64}, base, profile,
        )
        self.assertEqual(wrong_application.status_code, 422, wrong_application.text)
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         base["document_version"])
        for mutation, value in (("unreviewed", {"review_status": "DRAFT"}),
                                ("foreign-job", {"job_id": "foreign-job"})):
            with self.subTest(mutation=mutation):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.documents).where(
                        m.documents.c.id == reviewed["document_id"],
                    ).values(generation_context={**original_context, **value}))
                response = self._reprepare(reviewed, base, profile)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.store.application(self.application_id)["document_version"],
                                 base["document_version"])
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.documents).where(
                        m.documents.c.id == reviewed["document_id"],
                    ).values(generation_context=original_context))

        path = self.output / self.application_id / original["files"]["pdf"]
        original_bytes = path.read_bytes()
        for mutation in ("missing-file", "changed-file"):
            with self.subTest(mutation=mutation):
                path.unlink()
                if mutation == "changed-file":
                    path.write_bytes(original_bytes + b"changed")
                response = self._reprepare(reviewed, base, profile)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.store.application(self.application_id)["document_version"],
                                 base["document_version"])
                path.write_bytes(original_bytes)

    def test_failed_promotion_rolls_back_resume_and_retry_promotes_once(self):
        approved, base, profile = self._approve()
        self.assertEqual(approved.status_code, 201, approved.text)
        reviewed = approved.json()

        with patch("services.documents.generate._generate_document_artifact",
                   side_effect=RuntimeError("synthetic generation failure")):
            with self.assertRaises(RuntimeError):
                self._reprepare(reviewed, base, profile)
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         base["document_version"])
        generated_root = (self.output / self.application_id / profile["version"]
                          / str(reviewed["version"]))
        self.assertFalse(generated_root.exists())
        with self.store.engine.connect() as connection:
            future = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == reviewed["version"],
            )).mappings())
        self.assertEqual([row["kind"] for row in future], ["cover-letter"])

        retry = self._reprepare(reviewed, base, profile)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(generated_root.is_dir())
        with self.store.engine.connect() as connection:
            promoted = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == reviewed["version"],
            )).mappings())
        self.assertEqual(sorted(row["kind"] for row in promoted),
                         ["cover-letter", "resume"])

    def test_crash_after_review_publish_reconciles_matching_orphan_on_retry(self):
        self._crash_after_review_directory_publish()
        application = self.store.application(self.application_id)
        with self.store.engine.connect() as connection:
            future = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == application["document_version"] + 1,
            )).mappings())
        self.assertEqual(future, [])
        review_root = self.output / self.application_id / application["profile_version"]
        orphan_directories = list(review_root.glob("reviewed-*"))
        self.assertEqual(len(orphan_directories), 1)

        retry, _base, _profile = self._approve(
            request_id="reviewed-cover-letter-crash-window",
        )
        self.assertEqual(retry.status_code, 201, retry.text)
        with self.store.engine.connect() as connection:
            recovered = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == application["document_version"] + 1,
                m.documents.c.kind == "cover-letter",
            )).mappings())
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["id"], retry.json()["document_id"])
        self.assertEqual(list(review_root.glob("reviewed-*")), orphan_directories)

    def test_crash_orphan_with_changed_bytes_fails_closed(self):
        self._crash_after_review_directory_publish()
        application = self.store.application(self.application_id)
        review_root = self.output / self.application_id / application["profile_version"]
        orphan = next(review_root.glob("reviewed-*"))
        pdf = orphan / "cover-letter.pdf"
        pdf.write_bytes(pdf.read_bytes() + b"changed after crash")

        retry, _base, _profile = self._approve(
            request_id="reviewed-cover-letter-crash-window",
        )
        self.assertEqual(retry.status_code, 422, retry.text)
        self.assertEqual(retry.json()["detail"]["code"],
                         "COVER_LETTER_REVIEW_ORPHAN_MISMATCH")
        with self.store.engine.connect() as connection:
            future = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == application["document_version"] + 1,
            )).mappings())
        self.assertEqual(future, [])


if __name__ == "__main__":
    unittest.main()
