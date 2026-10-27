"""Criterion 42: an explicitly reviewed cover letter is the uploaded revision."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

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


class CoverLetterUploadCriterion42Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-criterion42-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "documents"
        self.store = Store(f"sqlite:///{(self.root / 'criterion42.db').as_posix()}")
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

    def _approve(self, *, request_id="criterion42-review-1", style="warm"):
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
            request_id="criterion42-review-2", style="direct",
        )
        self.assertEqual(competing.status_code, 409, competing.text)
        self.assertEqual(competing.json()["detail"]["code"],
                         "COVER_LETTER_REVIEW_VERSION_CONFLICT")
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         base["document_version"])

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
            "request_id": "criterion42-stale",
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
                request_id=f"criterion42-{name}-drift",
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


if __name__ == "__main__":
    unittest.main()
