"""Reviewed career-story references remain traceable beside immutable answer exports."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.career_profile.story_bank import create_story, review_story, revise_story
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationAnswerStoryCitationExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        profile = deepcopy(PROFILE)
        profile["answers"] = []
        profile.setdefault("facts", []).append({
            "id": "story-export-fact",
            "text": "Maintained synthetic Linux services for four test sites.",
            "kind": "accomplishment",
            "source": "Synthetic fixture work history",
            "verified": True,
            "user_approved": True,
            "confidence": 100,
        })
        self.store.save_profile(profile)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.export_url = f"/profiles/fixture/applications/{self.application_id}/answers/export"
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root))
        self.story = create_story(self.store, "fixture", {
            "kind": "STAR_STORY",
            "title": "Synthetic Linux service support",
            "text": "Maintained synthetic Linux services for four test sites.",
            "fact_ids": ["story-export-fact"],
            "tags": ["linux", "support"],
            "allowed_uses": ["APPLICATION_ANSWER"],
        })
        approved = review_story(
            self.store, "fixture", self.story["story_id"], True,
            "Synthetic acceptance review", expected_revision=1,
        )
        self.assertTrue(approved["reusable"])
        self.citation = {
            "item_index": 0,
            "story_id": self.story["story_id"],
            "revision": approved["revision"],
            "fact_provenance": approved["fact_fingerprints"],
        }

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_export_persists_provenance_only_citation_in_profile_scoped_history(self):
        response = self.client.post(self.export_url, json={
            "profile_id": "fixture",
            "items": [{
                "question": "Describe a Linux support accomplishment.",
                "text": "I maintained four synthetic Linux test sites.",
                "reviewed": True,
            }],
            "citations": [self.citation],
        })
        self.assertEqual(response.status_code, 201, response.text)
        document_id = response.json()["document_id"]

        exported = self.client.get(f"/profiles/fixture/documents/{document_id}/txt")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("I maintained four synthetic Linux test sites.", exported.text)
        self.assertNotIn("Maintained synthetic Linux services for four test sites.", exported.text)

        history = self.client.get(
            f"/profiles/fixture/applications/{self.application_id}/documents")
        self.assertEqual(history.status_code, 200, history.text)
        document = next(row for row in history.json()["documents"] if row["id"] == document_id)
        self.assertEqual(document["story_citations"], [{
            "item_index": 0,
            "story_id": self.story["story_id"],
            "revision": 1,
            "fact_provenance": self.citation["fact_provenance"],
        }])

        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_ANSWER_STORY_CITATIONS_RECORDED",
            )).scalars())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["document_id"], document_id)
        self.assertNotIn("text", events[0]["citations"][0])
        self.assertNotIn("answer", str(events[0]).casefold())
        self.assertEqual(self.client.get(
            f"/profiles/other/applications/{self.application_id}/documents").status_code, 404)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root))
        reloaded = self.client.get(
            f"/profiles/fixture/applications/{self.application_id}/documents")
        self.assertEqual(reloaded.status_code, 200, reloaded.text)
        saved_again = next(row for row in reloaded.json()["documents"] if row["id"] == document_id)
        self.assertEqual(saved_again["story_citations"], document["story_citations"])

    def test_stale_citation_is_rejected_before_creating_an_export(self):
        revise_story(self.store, "fixture", self.story["story_id"], {
            "kind": "STAR_STORY",
            "title": "Pending changed story",
            "text": "Changed synthetic story text.",
            "fact_ids": ["story-export-fact"],
            "tags": ["linux"],
            "allowed_uses": ["APPLICATION_ANSWER"],
        }, expected_revision=1)
        response = self.client.post(self.export_url, json={
            "profile_id": "fixture",
            "items": [{
                "question": "Describe a Linux support accomplishment.",
                "text": "Reviewed answer text.",
                "reviewed": True,
            }],
            "citations": [self.citation],
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], {
            "code": "APPLICATION_ANSWER_STORY_CITATION_NOT_CURRENT",
        })
        history = self.client.get(
            f"/profiles/fixture/applications/{self.application_id}/documents")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertFalse(any(row["kind"] == "application-answer-export"
                             for row in history.json()["documents"]))

    def test_sensitive_question_rejects_citation_without_export_or_audit(self):
        response = self.client.post(self.export_url, json={
            "profile_id": "fixture",
            "items": [{
                "question": "Are you authorized to work in the United States?",
                "text": "I will answer this separately.",
                "reviewed": True,
            }],
            "citations": [self.citation],
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], {
            "code": "APPLICATION_ANSWER_STORY_CITATION_SENSITIVE_QUESTION",
        })
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_ANSWER_STORY_CITATIONS_RECORDED",
            )).all(), [])

    def test_tampered_fact_fingerprint_is_rejected_before_export(self):
        tampered = {**self.citation, "fact_provenance": [{
            "fact_id": "story-export-fact",
            "fingerprint": "0" * 64,
        }]}
        response = self.client.post(self.export_url, json={
            "profile_id": "fixture",
            "items": [{
                "question": "Describe a Linux support accomplishment.",
                "text": "Reviewed answer text.",
                "reviewed": True,
            }],
            "citations": [tampered],
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], {
            "code": "APPLICATION_ANSWER_STORY_CITATION_NOT_CURRENT",
        })
        history = self.client.get(
            f"/profiles/fixture/applications/{self.application_id}/documents")
        self.assertFalse(any(row["kind"] == "application-answer-export"
                             for row in history.json()["documents"]))


if __name__ == "__main__":
    unittest.main()
