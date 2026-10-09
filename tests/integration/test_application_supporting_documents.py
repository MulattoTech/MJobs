"""Profile- and job-scoped selections for local mock application documents."""
from pathlib import Path
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from sqlalchemy import select, update


class ApplicationSupportingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-application-supporting-docs-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'supporting-docs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            operation_mode="FIXTURE",
        ))
        self.url = (
            f"/profiles/fixture/applications/{self.application_id}/supporting-documents"
        )

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _upload(self, kind="SUPPORTING_DOCUMENT", profile="fixture"):
        return self.client.post(
            f"/profiles/{profile}/document-library",
            content=b"Synthetic, unreviewed supporting document.",
            headers={
                "Content-Type": "text/plain",
                "X-Filename": "supporting-evidence.txt",
                "X-Document-Kind": kind,
                "X-Document-Label": "Synthetic supporting evidence",
            },
        )

    def test_profile_owned_selection_is_durable_job_scoped_and_idempotent(self):
        uploaded = self._upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        revision_id = uploaded.json()["revision_id"]

        before = self.store.application(self.application_id)
        empty = self.client.get(self.url)
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["selected_revision_ids"], [])
        self.assertEqual([row["revision_id"] for row in empty.json()["available"]], [revision_id])

        payload = {"revision_ids": [revision_id], "request_id": "supporting-selection-1"}
        saved = self.client.put(self.url, json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["selected_revision_ids"], [revision_id])
        self.assertFalse(saved.json()["replayed"])
        replay = self.client.put(self.url, json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(
            {key: value for key, value in replay.json().items() if key != "replayed"},
            {key: value for key, value in saved.json().items() if key != "replayed"},
        )

        changed_replay = self.client.put(self.url, json={
            **payload, "revision_ids": [],
        })
        self.assertEqual(changed_replay.status_code, 409, changed_replay.text)
        reread = self.client.get(self.url)
        self.assertEqual(reread.json()["selected_revision_ids"], [revision_id])
        self.assertEqual(self.store.application(self.application_id)["state"], before["state"])
        self.assertIsNone(self.store.application(self.application_id)["confirmation"])
        self.assertEqual(self.client.get(
            f"/profiles/other/applications/{self.application_id}/supporting-documents"
        ).status_code, 404)
        self.assertEqual(self.client.put(
            f"/profiles/other/applications/{self.application_id}/supporting-documents",
            json={"revision_ids": [revision_id], "request_id": "foreign-selection"},
        ).status_code, 404)

    def test_rejects_wrong_document_kind_and_foreign_revision(self):
        base_resume = self._upload("BASE_RESUME").json()["revision_id"]
        foreign = self._upload(profile="other").json()["revision_id"]
        invalid = self.client.put(self.url, json={
            "revision_ids": [base_resume], "request_id": "base-resume-as-support",
        })
        self.assertEqual(invalid.status_code, 422, invalid.text)
        invalid = self.client.put(self.url, json={
            "revision_ids": [foreign], "request_id": "foreign-supporting-document",
        })
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(self.client.get(self.url).json()["selected_revision_ids"], [])

    def test_selection_isolated_to_the_exact_prepared_job(self):
        uploaded = self._upload()
        revision_id = uploaded.json()["revision_id"]
        selected = self.client.put(self.url, json={
            "revision_ids": [revision_id], "request_id": "job-specific-selection",
        })
        self.assertEqual(selected.status_code, 200, selected.text)

        other_job = next(job for job in self.store.jobs("fixture")
                         if job["id"] != self.job_id)
        other_application = prepare(self.store, other_job["id"], "fixture", self.root)
        other_url = (
            f"/profiles/fixture/applications/{other_application}/supporting-documents"
        )
        other_selection = self.client.get(other_url)
        self.assertEqual(other_selection.status_code, 200, other_selection.text)
        self.assertEqual(other_selection.json()["selected_revision_ids"], [])

    def test_mock_dry_run_uploads_only_selected_file_and_stops_before_submission(self):
        uploaded = self._upload()
        revision = uploaded.json()
        selection = self.client.put(self.url, json={
            "revision_ids": [revision["revision_id"]],
            "request_id": "mock-supporting-upload",
        })
        self.assertEqual(selection.status_code, 200, selection.text)

        from services.applications.mock_adapter import apply_mock
        with fixture_server() as origin:
            with self.store.engine.begin() as connection:
                route_id = self.store.application(self.application_id)["route_id"]
                route = connection.execute(select(m.routes).where(
                    m.routes.c.id == route_id,
                )).mappings().one()
                connection.execute(update(m.routes).where(
                    m.routes.c.id == route_id,
                ).values(url=f"{origin}/ats/apply/{route['requisition']}"))

            result = apply_mock(
                self.store, self.application_id, origin, self.root, submit=False,
            )
            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
            with self.store.engine.connect() as connection:
                attempts = connection.execute(select(m.attempts.c.steps).where(
                    m.attempts.c.application_id == self.application_id,
                )).scalar_one()
            self.assertIn("UPLOAD_SUPPORTING_DOCUMENTS:COMPLETED", attempts)
            import httpx
            self.assertEqual(httpx.get(origin + "/_test/request-counts").json(),
                             {"review": 1, "submit": 0})
            self.assertEqual(httpx.get(origin + "/_test/supporting-uploads").json(), {
                "uploads": [{
                    "filename": "supporting-evidence.txt",
                    "size_bytes": len(b"Synthetic, unreviewed supporting document."),
                    "sha256": revision["sha256"],
                }],
            })

        with self.store.engine.connect() as connection:
            event = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_SUPPORTING_DOCUMENTS_SELECTED",
            )).mappings().one()
        self.assertNotIn("Synthetic, unreviewed", str(event["payload"]))
        self.assertNotIn("supporting-evidence.txt", str(event["payload"]))

    def test_archived_selected_revision_requires_review_before_mock_network_access(self):
        from services.applications.mock_adapter import apply_mock

        uploaded = self._upload()
        revision_id = uploaded.json()["revision_id"]
        self.assertEqual(self.client.put(self.url, json={
            "revision_ids": [revision_id], "request_id": "archive-gate-selection",
        }).status_code, 200)
        archived = self.client.post(
            f"/profiles/fixture/document-library/{revision_id}/archive",
            headers={"Idempotency-Key": "archive-before-mock-upload"},
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        view = self.client.get(self.url).json()
        self.assertEqual(view["selected_revision_ids"], [revision_id])
        self.assertEqual(view["unavailable_revision_ids"], [revision_id])

        with fixture_server() as origin:
            with self.store.engine.begin() as connection:
                route_id = self.store.application(self.application_id)["route_id"]
                route = connection.execute(select(m.routes).where(
                    m.routes.c.id == route_id,
                )).mappings().one()
                connection.execute(update(m.routes).where(
                    m.routes.c.id == route_id,
                ).values(url=f"{origin}/ats/apply/{route['requisition']}"))
            with self.assertRaisesRegex(ValueError, "SUPPORTING_DOCUMENT_UNAVAILABLE"):
                apply_mock(self.store, self.application_id, origin, self.root, submit=False)
            self.assertEqual(httpx.get(origin + "/_test/request-counts").json(),
                             {"review": 0, "submit": 0})
        self.assertEqual(self.store.application(self.application_id)["state"], "REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
