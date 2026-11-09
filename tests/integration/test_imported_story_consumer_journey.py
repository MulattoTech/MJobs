"""Imported stories retain reviewed identity across real durable consumers."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.documents.errors import PreparationError
from services.documents.generate import prepare
from services.documents.history import (
    validated_letter_preview,
    validated_resume_preview,
)
from tests.integration.test_spine import PROFILE


class ImportedStoryConsumerJourneyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-imported-story-journey-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "documents"
        self.output.mkdir()
        self.database_url = f"sqlite:///{(self.root / 'stories.sqlite').as_posix()}"
        self.store = None
        self.client = None
        self.addCleanup(self._close)
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        profile = deepcopy(PROFILE)
        profile["answers"] = []
        profile["facts"].append({
            "id": "imported-story-fact",
            "text": "Maintained synthetic Python reporting services for four test teams.",
            "kind": "accomplishment",
            "source": "Synthetic imported-story fixture",
            "verified": True,
            "user_approved": True,
            "confidence": 100,
        })
        self.store.save_profile(profile)
        self.job_id = identity("imported-story-journey-job")
        company_id = identity("imported-story-journey-company")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": "Synthetic Reporting Systems",
                "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": self.job_id, "company_id": company_id,
                "requisition": "imported-story-journey", "title": "Python Support Engineer",
                "description": "Responsibilities:\nMaintain synthetic Python reporting services.",
                "skills": ["Python"], "location": "Remote", "remote_type": "REMOTE",
                "attributes": {},
            })
            # This route is metadata only: no server, worker or submission runs.
            upsert(connection, m.routes, {
                "id": identity("imported-story-journey-route"), "job_id": self.job_id,
                "company_id": company_id, "provider": "mock-ats", "tenant": "synthetic",
                "requisition": "imported-story-journey",
                "url": "https://jobs.example.test/imported-story-journey",
            })
        self.original_profile = deepcopy(self.store.profile("fixture"))
        self.original_facts = self._rows(m.facts)
        self._open_client()
        # A real owned application lets pending answer citations reach their
        # actual gate. Later preparation must create story-bearing revisions.
        self.application_id = prepare(self.store, self.job_id, "fixture", self.output)
        self.history_url = f"/profiles/fixture/applications/{self.application_id}/documents"
        self.answer_url = f"/profiles/fixture/applications/{self.application_id}/answers/export"
        self.pack_url = f"/profiles/fixture/jobs/{self.job_id}/interview-prep-packs"
        self.story_payload = {
            "kind": "STAR_STORY",
            "title": "Synthetic reporting support",
            "text": "Situation: four synthetic test teams needed reporting support.\n"
                    "Action: maintained Python reporting services.\n"
                    "Result: those teams retained their reporting service.",
            "fact_ids": ["imported-story-fact"],
            "tags": ["python", "reporting"],
            "allowed_uses": ["RESUME", "COVER_LETTER", "APPLICATION_ANSWER",
                             "INTERVIEW_PREPARATION"],
        }

    def _open_client(self):
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.output, operation_mode="PERSONAL"))

    def _close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.store is not None:
            self.store.engine.dispose()
            self.store = None

    def _reopen(self):
        self._close()
        self.store = Store(self.database_url)
        self.store.migrate()
        self._open_client()

    def _rows(self, table):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id)).mappings()]

    def _files(self):
        return {
            path.relative_to(self.output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.output.rglob("*") if path.is_file()
        }

    def _consumer_snapshot(self):
        tables = (m.applications, m.documents, m.interview_prep_packs, m.career_stories,
                  m.audit, m.attempts, m.operations)
        return {"rows": {table.name: self._rows(table) for table in tables},
                "files": self._files()}

    def _assert_facts_unchanged(self):
        self.assertEqual(self.store.profile("fixture"), self.original_profile)
        self.assertEqual(self._rows(m.facts), self.original_facts)

    def _import(self):
        response = self.client.post("/profiles/fixture/career-stories/import", json={
            "schema_version": 1, "stories": [self.story_payload],
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _selection(self, allowed_use):
        response = self.client.get(
            f"/profiles/fixture/jobs/{self.job_id}/career-stories/selectable",
            params={"allowed_use": allowed_use},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def _citation(story):
        return {"item_index": 0, "story_id": story["story_id"],
                "revision": story["revision"],
                "fact_provenance": story["fact_fingerprints"]}

    def _answer(self, story):
        return self.client.post(self.answer_url, json={
            "profile_id": "fixture",
            "items": [{"question": "Describe a Python reporting accomplishment.",
                       "text": "I maintained synthetic Python reporting services for four test teams.",
                       "reviewed": True}],
            "citations": [self._citation(story)],
        })

    def _assert_unavailable(self, story):
        before = self._consumer_snapshot()
        for allowed_use, prefix in (("RESUME", "resume"), ("COVER_LETTER", "cover_letter")):
            selection = self._selection(allowed_use)
            self.assertNotIn(story["story_id"], [row["story_id"] for row in selection["stories"]])
            with self.assertRaises(PreparationError) as rejected:
                prepare(self.store, self.job_id, "fixture", self.output,
                        profile_version=selection["profile_version"], **{
                            f"{prefix}_story_ids": [story["story_id"]],
                            f"{prefix}_story_job_fingerprint": selection["job_fingerprint"],
                        })
            self.assertEqual(rejected.exception.code, "STORY_NOT_SELECTABLE")
        answer = self._answer(story)
        self.assertEqual(answer.status_code, 422, answer.text)
        self.assertEqual(answer.json()["detail"], {
            "code": "APPLICATION_ANSWER_STORY_CITATION_NOT_CURRENT",
        })
        pack = self.client.post(self.pack_url, json={
            "selected_story_revision_ids": [story["story_id"]],
        })
        self.assertEqual(pack.status_code, 422, pack.text)
        self.assertEqual(pack.json()["detail"], {
            "code": "INTERVIEW_PREPARATION_STORY_NOT_SELECTABLE",
        })
        self.assertEqual(self._consumer_snapshot(), before,
                         "Rejected story reuse must not create or alter durable output")
        self._assert_facts_unchanged()

    def _history(self):
        response = self.client.get(self.history_url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["documents"]

    def _assert_durable_consumers(self, story, generated, exported, pack):
        before_reads = self._consumer_snapshot()
        history = {row["id"]: row for row in self._history()}
        for original in generated:
            document = history[original["id"]]
            self.assertEqual(document["sha256"], original["sha256"])
            self.assertEqual(document["generation_context"], original["generation_context"])
            self.assertIsNone(document["preview_error"])
            preview = (validated_resume_preview if document["kind"] == "resume"
                       else validated_letter_preview)
            self.assertIn(story["text"], preview(self.store, document, self.output))
            for format_name, relative in document["files"].items():
                path = self.output / self.application_id / relative
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                                 document["sha256"][format_name])
            docx_path = self.output / self.application_id / document["files"]["docx"]
            self.assertIn(story["text"], "\n".join(
                paragraph.text for paragraph in Document(docx_path).paragraphs))
        answer_document = history[exported["document_id"]]
        self.assertEqual(answer_document["story_citations"], [self._citation(story)])
        self.assertEqual(answer_document["sha256"]["txt"], exported["sha256"])
        answer = self.client.get(f"/profiles/fixture/documents/{exported['document_id']}/txt")
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIn("I maintained synthetic Python reporting services for four test teams.", answer.text)
        self.assertNotIn(story["text"], answer.text)
        self.assertEqual(hashlib.sha256(answer.content).hexdigest(), exported["sha256"])
        detail = self.client.get(f"{self.pack_url}/{pack['pack_id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json(), pack)
        self.assertEqual(self._consumer_snapshot(), before_reads,
                         "History, downloads and immutable previews must be read-only")
        self._assert_facts_unchanged()

    def test_import_review_consume_reopen_and_edit_preserve_exact_story_lineage(self):
        receipt = self._import()
        self.assertEqual(receipt["created_count"], 1)
        [imported] = receipt["stories"]
        self.assertEqual(imported["revision"], 1)
        self.assertEqual(imported["review_status"], "PENDING")
        self.assertFalse(imported["reusable"])
        self.assertEqual(imported["fact_ids"], ["imported-story-fact"])
        self.assertEqual(imported["import_origin"], {
            "schema_version": 1, "canonical_sha256": receipt["canonical_sha256"],
            "record_index": 0,
        })
        self._assert_facts_unchanged()
        self._assert_unavailable(imported)

        review = self.client.post(
            f"/profiles/fixture/career-stories/{imported['story_id']}/review",
            json={"approved": True, "expected_revision": 1,
                  "reviewer_note": "Checked against the approved synthetic fact."},
        )
        self.assertEqual(review.status_code, 200, review.text)
        approved = review.json()
        self.assertEqual(approved["review_status"], "APPROVED")
        self.assertTrue(approved["reusable"])
        self.assertTrue(approved["reviewed_at"])
        for key in ("story_id", "story_key", "revision", "text", "fact_fingerprints", "import_origin"):
            self.assertEqual(approved[key], imported[key])
        selections = [self._selection(use) for use in ("RESUME", "COVER_LETTER")]
        for selection in selections:
            self.assertEqual([row["story_id"] for row in selection["stories"]], [approved["story_id"]])
            self.assertEqual(selection["stories"][0]["fact_fingerprints"], approved["fact_fingerprints"])
        self.assertEqual(selections[0]["job_fingerprint"], selections[1]["job_fingerprint"])
        prior_version = self.store.application(self.application_id)["document_version"]
        prepared = prepare(
            self.store, self.job_id, "fixture", self.output,
            profile_version=selections[0]["profile_version"],
            resume_story_ids=[approved["story_id"]],
            resume_story_job_fingerprint=selections[0]["job_fingerprint"],
            cover_letter_story_ids=[approved["story_id"]],
            cover_letter_story_job_fingerprint=selections[1]["job_fingerprint"],
        )
        self.assertEqual(prepared, self.application_id)
        application = self.store.application(self.application_id)
        self.assertEqual(application["document_version"], prior_version + 1)
        self.assertEqual(application["state"], "PREPARED")
        self.assertEqual(application["target"], "MOCK")
        generated = [row for row in self._history()
                     if row["version"] == application["document_version"]
                     and row["kind"] in {"resume", "cover-letter"}]
        self.assertEqual({row["kind"] for row in generated}, {"resume", "cover-letter"})
        expected_ref = {
            "story_id": approved["story_id"], "revision": 1,
            "title": approved["title"], "profile_version": approved["profile_version"],
            "fact_ids": approved["fact_ids"], "fact_fingerprints": approved["fact_fingerprints"],
            "text_sha256": hashlib.sha256(approved["text"].encode("utf-8")).hexdigest(),
        }
        for document in generated:
            self.assertEqual(document["generation_context"]["story_refs"], [expected_ref])

        answer = self._answer(approved)
        self.assertEqual(answer.status_code, 201, answer.text)
        exported = answer.json()
        self.assertEqual(exported["external_submission"], "OFF")
        self.assertEqual(exported["story_citation_count"], 1)
        citations = [row["payload"] for row in self._rows(m.audit)
                     if row["action"] == "APPLICATION_ANSWER_STORY_CITATIONS_RECORDED"]
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["document_id"], exported["document_id"])
        self.assertEqual(citations[0]["citations"], [self._citation(approved)])
        self.assertEqual(set(citations[0]["citations"][0]),
                         {"item_index", "story_id", "revision", "fact_provenance"})
        self.assertNotIn(approved["text"], str(citations[0]))

        response = self.client.post(self.pack_url, json={
            "selected_story_revision_ids": [approved["story_id"]],
        })
        self.assertEqual(response.status_code, 200, response.text)
        pack = response.json()
        self.assertEqual(pack["selected_story_revision_ids"], [approved["story_id"]])
        expected_sources = [f"career_story_revision:{approved['story_id']}",
                            "career_fact:imported-story-fact"]
        self.assertEqual(pack["talking_points"], [{
            "text": approved["text"], "source_refs": expected_sources,
        }])
        self.assertEqual(pack["star_prompts"][0]["source_refs"], expected_sources)
        self.assertTrue(any(item["source_refs"] == expected_sources for item in pack["questions"]))
        self.assertNotIn("generated_answer", pack)
        repeat = self.client.post(self.pack_url, json={
            "selected_story_revision_ids": [approved["story_id"]],
        })
        self.assertEqual(repeat.status_code, 200, repeat.text)
        self.assertEqual(repeat.json(), pack)
        self.assertEqual(len(self._rows(m.interview_prep_packs)), 1)
        self._assert_durable_consumers(approved, generated, exported, pack)

        self._reopen()
        listed = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["stories"], [approved])
        self._assert_durable_consumers(approved, generated, exported, pack)

        revised = self.client.post(
            f"/profiles/fixture/career-stories/{approved['story_id']}/versions",
            json={**self.story_payload, "expected_revision": 1,
                  "title": "Updated synthetic reporting account",
                  "text": "Updated synthetic account of maintaining Python reporting services."},
        )
        self.assertEqual(revised.status_code, 201, revised.text)
        pending = revised.json()
        self.assertEqual(pending["story_key"], approved["story_key"])
        self.assertNotEqual(pending["story_id"], approved["story_id"])
        self.assertEqual(pending["revision"], 2)
        self.assertEqual(pending["review_status"], "PENDING")
        self.assertFalse(pending["reusable"])
        self._assert_unavailable(approved)
        self._assert_unavailable(pending)
        replay = self._import()
        self.assertEqual(replay["created_count"], 0)
        self.assertEqual(replay["canonical_sha256"], receipt["canonical_sha256"])
        [original] = replay["stories"]
        self.assertEqual(original["story_id"], approved["story_id"])
        self.assertEqual(original["revision"], 1)
        self.assertEqual(original["review_status"], "APPROVED")
        self.assertFalse(original["is_latest"])
        self.assertFalse(original["reusable"])
        for key in ("text", "fact_fingerprints", "reviewed_at", "import_origin"):
            self.assertEqual(original[key], approved[key])
        self.assertEqual(len(self._rows(m.career_stories)), 2)

        self._reopen()
        listed = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(listed.status_code, 200, listed.text)
        revisions = {row["revision"]: row for row in listed.json()["stories"]}
        self.assertEqual(revisions[2], pending)
        self.assertFalse(revisions[1]["is_latest"])
        self.assertFalse(revisions[1]["reusable"])
        for key in ("story_id", "text", "fact_fingerprints", "review_status", "reviewed_at"):
            self.assertEqual(revisions[1][key], approved[key])
        # Earlier outputs retain their approved immutable source, even though
        # neither historical revision 1 nor pending revision 2 is newly reusable.
        self._assert_durable_consumers(approved, generated, exported, pack)
        self.assertEqual(self._rows(m.attempts), [])
        self.assertEqual(self._rows(m.operations), [])


if __name__ == "__main__":
    unittest.main()
