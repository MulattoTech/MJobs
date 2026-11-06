"""Cross-phase document identity, upload, and restart provenance acceptance."""
import hashlib
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.applications.attempt_history import read_attempt_history
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class JourneyDocumentProvenanceTests(unittest.TestCase):
    def test_truthful_application_documents_and_uploaded_base_survive_upload_and_reopen(self):
        with ExitStack() as stack:
            directory = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="mjobs-journey-document-provenance-")
            )
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'journey.db').as_posix()}"
            store = Store(database_url)
            stack.callback(store.engine.dispose)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            pending_id = "pending-private-project"
            pending_text = "Built an unapproved lunar telemetry platform."
            facts = [*PROFILE["facts"], dict(
                PROFILE["facts"][0], id=pending_id, kind="project", text=pending_text,
                verified=False, user_approved=False,
            )]
            store.save_profile(dict(PROFILE, id="fixture", facts=facts))
            profile_before = store.profile("fixture")

            origin = stack.enter_context(fixture_server())
            discover(store, origin, "fixture")
            job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
            application_id = prepare(store, job["id"], "fixture", root)

            with TestClient(create_app(store, origin, root,
                    "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as api:
                uploaded_base_bytes = (
                    b"Immutable uploaded base resume\n"
                    b"Source-only sentinel: preserve this original byte sequence.\n"
                )
                uploaded_response = api.post(
                    "/profiles/fixture/resumes", content=uploaded_base_bytes,
                    headers={"Content-Type": "text/plain", "X-Filename": "base.txt"},
                )
                self.assertEqual(uploaded_response.status_code, 201, uploaded_response.text)
                uploaded_base = uploaded_response.json()

                application = store.application(application_id)
                self.assertEqual(application["profile_id"], "fixture")
                self.assertEqual(application["job_id"], job["id"])
                documents = api.get(
                    f"/profiles/fixture/applications/{application_id}/documents"
                ).json()["documents"]
                current_documents = [
                    row for row in documents if row["version"] == application["document_version"]
                ]
                self.assertEqual({row["kind"] for row in current_documents},
                                 {"resume", "cover-letter"})
                resume = next(row for row in current_documents if row["kind"] == "resume")
                cover = next(row for row in current_documents if row["kind"] == "cover-letter")
                self.assertNotEqual(resume["id"], cover["id"])
                self.assertNotEqual(resume["sha256"]["pdf"], cover["sha256"]["pdf"])
                approved_fact_ids = {
                    row["fact_id"] for row in profile_before["facts"]
                    if row["verified"] and row["user_approved"]
                }
                for document in (resume, cover):
                    self.assertEqual(document["profile_id"], application["profile_id"])
                    self.assertEqual(document["job_id"], application["job_id"])
                    self.assertEqual(document["profile_version"], application["profile_version"])
                    self.assertEqual(document["version"], application["document_version"])
                    self.assertTrue(set(document["fact_ids"]).issubset(approved_fact_ids))
                    self.assertNotIn(pending_id, document["fact_ids"])
                    self.assertNotIn(pending_id, {row["fact_id"] for row in document["facts_used"]})
                    self.assertNotIn(pending_text, document.get("preview_text", ""))

                expected_uploads = {
                    ("resume", resume["id"]): resume,
                    ("cover-letter", cover["id"]): cover,
                }
                self.assertEqual(store.profile("fixture"), profile_before)
                self.assertEqual(uploaded_base["profile_id"], "fixture")
                self.assertEqual(uploaded_base["sha256"], hashlib.sha256(uploaded_base_bytes).hexdigest())
                self.assertEqual(uploaded_base["version"], 1)

                result = apply_mock(
                    store, application_id, origin, root, request_id="journey-provenance-upload",
                )
                preflight = api.get(
                    f"/profiles/fixture/applications/{application_id}"
                ).json().get("question_preflight")
                self.assertEqual(result["state"], "DRY_RUN_COMPLETE",
                                 {"application": result, "question_preflight": preflight})
                initial_history = read_attempt_history(store, "fixture", application_id)
                self.assertEqual(initial_history["application_id"], application_id)
                self.assertEqual(initial_history["profile_id"], "fixture")
                self.assertEqual(len(initial_history["attempts"]), 1)
                attempt = initial_history["attempts"][0]
                self.assertEqual(attempt["profile_version"], application["profile_version"])
                self.assertEqual(attempt["document_version"], application["document_version"])
                manifest = attempt["verified_uploads"]
                self.assertEqual({(row["kind"], row["document_id"]) for row in manifest},
                                 set(expected_uploads))
                for entry in manifest:
                    document = expected_uploads[(entry["kind"], entry["document_id"])]
                    pdf_path = root / application_id / document["files"]["pdf"]
                    self.assertEqual(entry["sha256"], document["sha256"]["pdf"])
                    self.assertEqual(entry["size_bytes"], pdf_path.stat().st_size)

            store.engine.dispose()
            reopened = Store(database_url)
            stack.callback(reopened.engine.dispose)
            try:
                recovered_application = reopened.application(application_id)
                self.assertEqual(recovered_application["profile_id"], "fixture")
                self.assertEqual(recovered_application["job_id"], job["id"])
                self.assertEqual(recovered_application["document_version"], application["document_version"])
                recovered_history = read_attempt_history(reopened, "fixture", application_id)
                self.assertEqual(recovered_history["attempts"][0]["verified_uploads"], manifest)
                self.assertEqual(recovered_history["attempts"][0]["profile_version"],
                                 application["profile_version"])
                self.assertEqual(recovered_history["attempts"][0]["document_version"],
                                 application["document_version"])

                with TestClient(create_app(reopened, origin, root,
                        "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as recovered_api:
                    recovered_documents = recovered_api.get(
                        f"/profiles/fixture/applications/{application_id}/documents"
                    ).json()["documents"]
                    recovered_current = [
                        row for row in recovered_documents
                        if row["version"] == recovered_application["document_version"]
                    ]
                    self.assertEqual({(row["kind"], row["id"]) for row in recovered_current},
                                     set(expected_uploads))
                    for (kind, document_id), before in expected_uploads.items():
                        after = next(row for row in recovered_current if row["id"] == document_id)
                        self.assertEqual(after["profile_id"], "fixture")
                        self.assertEqual(after["job_id"], job["id"])
                        self.assertEqual(after["profile_version"], application["profile_version"])
                        self.assertEqual(after["fact_ids"], before["fact_ids"])
                        for extension in ("pdf", "txt"):
                            downloaded = recovered_api.get(
                                f"/profiles/fixture/documents/{document_id}/{extension}"
                            )
                            self.assertEqual(downloaded.status_code, 200, downloaded.text)
                            self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(),
                                             before["sha256"][extension])
                            if extension == "txt":
                                self.assertNotIn(pending_text.encode(), downloaded.content)
                                for fact in after["facts_used"]:
                                    self.assertIn(fact["text"].encode(), downloaded.content)

                    recovered_base_revisions = recovered_api.get(
                        "/profiles/fixture/resumes"
                    ).json()["resumes"]
                    recovered_base_row = next(
                        row for row in recovered_base_revisions
                        if row["id"] == uploaded_base["id"]
                    )
                    self.assertEqual(recovered_base_row["revision_id"], uploaded_base["revision_id"])
                    self.assertEqual(recovered_base_row["version"], uploaded_base["version"])
                    recovered_base = recovered_api.get(
                        f"/profiles/fixture/resumes/{uploaded_base['id']}"
                    )
                    self.assertEqual(recovered_base.status_code, 200, recovered_base.text)
                    self.assertEqual(recovered_base.content, uploaded_base_bytes)
                    self.assertEqual(hashlib.sha256(recovered_base.content).hexdigest(),
                                     uploaded_base["sha256"])
                    self.assertEqual(recovered_api.get("/profiles/fixture").json(), profile_before)
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
