"""Application preview inputs come from durable profile-scoped readbacks."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import ApplicationAnswerItem, create_app
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.answers import export_answers
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationPreviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'preview.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_review_inputs_are_durable_and_read_only_when_missing_preflight_is_unknown(self):
        with fixture_server() as fixture:
            discover(self.store, fixture, "fixture")
            job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
            application_id = prepare(self.store, job["id"], "fixture", self.root)
        export_answers(self.store, application_id, self.root, [ApplicationAnswerItem(**{
            "question": "Describe a fictional project.",
            "text": "I built a small synthetic Python project.",
            "reviewed": True,
        })])
        state_before = self.store.application(application_id)
        documents_before = self.store.count("documents")
        attempts_before = self.store.count("application_attempts")
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")

        with TestClient(app) as client:
            application = client.get(f"/profiles/fixture/applications/{application_id}")
            documents = client.get(f"/profiles/fixture/applications/{application_id}/documents")
            answers = client.get(f"/profiles/fixture/applications/{application_id}/answers")
            tracking = client.get(f"/profiles/fixture/applications/{application_id}/tracking")
            stored = documents.json()["documents"]
            answer_export = next(row for row in stored if row["kind"] == "application-answer-export")
            answer_text = client.get(f"/profiles/fixture/documents/{answer_export['id']}/txt")

        self.assertEqual(application.status_code, 200)
        self.assertEqual(application.json()["id"], application_id)
        self.assertTrue(application.json()["route_id"])
        self.assertTrue(application.json()["route_choices"])
        self.assertIsNone(application.json()["question_preflight"])
        self.assertIn("adapter_status", application.json()["adapter_knowledge"])
        self.assertEqual(answers.status_code, 200)
        self.assertEqual(answers.json()["answers"], PROFILE["answers"])
        self.assertEqual(tracking.status_code, 200)
        self.assertEqual({row["kind"] for row in stored}, {
            "resume", "cover-letter", "application-answer-export"})
        self.assertIn("Describe a fictional project.", answer_text.text)
        self.assertIn("I built a small synthetic Python project.", answer_text.text)
        self.assertEqual(self.store.application(application_id), state_before)
        self.assertEqual(self.store.count("documents"), documents_before)
        self.assertEqual(self.store.count("application_attempts"), attempts_before)


if __name__ == "__main__":
    unittest.main()
