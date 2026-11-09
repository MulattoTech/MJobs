"""Real API exports cannot bypass the matching service's sensitive reuse gate."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


PARAPHRASES = (
    "Have you served in the armed forces?",
    "What is your national origin?",
    "What is your expected salary?",
    "Do you have restrictive covenants?",
    "What legal certifications do you hold?",
    "Are you at least 18 years old?",
)


def approved_answer(answer_id, question, text="SYNTHETIC_SAVED_ANSWER"):
    return {"id": answer_id, "question": question, "text": text,
            "source": "Synthetic answer-boundary fixture", "verified": True,
            "user_approved": True, "confidence": 100}


class AnswerExportSensitiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-sensitive-export-boundary-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "documents"
        self.output.mkdir()
        self.url = f"sqlite:///{(self.root / 'answers.sqlite').as_posix()}"
        self.store = Store(self.url)
        self.client = None
        self.addCleanup(self._close)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile(dict(deepcopy(PROFILE), id="other", name="Other fixture"))
        company_id, job_id = identity("sensitive-export-company"), identity("sensitive-export-job")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": "Synthetic Answer Employer", "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": job_id, "company_id": company_id, "requisition": "sensitive-export",
                "title": "Synthetic Python Engineer",
                "description": "Responsibilities:\nMaintain synthetic Python reporting services.",
                "skills": ["Python"], "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            # Route metadata only: the tests never start a provider, browser or worker.
            upsert(connection, m.routes, {
                "id": identity("sensitive-export-route"), "job_id": job_id,
                "company_id": company_id, "provider": "mock-ats", "tenant": "synthetic",
                "requisition": "sensitive-export", "url": "https://jobs.example.test/sensitive-export",
            })
        self.application_id = prepare(self.store, job_id, "fixture", self.output)
        self.base = f"/profiles/fixture/applications/{self.application_id}"
        self._open_client()

    def _open_client(self):
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.output))

    def _close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.store is not None:
            self.store.engine.dispose()
            self.store = None

    def _reopen(self):
        self._close()
        self.store = Store(self.url)
        self._open_client()

    def _save_answers(self, answers, policies=None):
        profile = deepcopy(PROFILE)
        profile.update(answers=answers, preferences={"sensitive_answer_policies": policies or {}},
                       expected_version=self.store.profile("fixture")["version"])
        result = self.client.put("/profiles", json=profile)
        self.assertEqual(result.status_code, 200, result.text)

    def _export(self, question, text, answer_id=None, *, reviewed=True, profile_id="fixture"):
        item = {"question": question, "text": text, "reviewed": reviewed}
        if answer_id is not None:
            item["answer_id"] = answer_id
        return self.client.post(self.base + "/answers/export", json={
            "profile_id": profile_id, "items": [item],
        })

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            rows = {table.name: [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id)).mappings()]
                for table in (m.documents, m.audit, m.applications, m.attempts, m.operations)}
        files = {path.relative_to(self.output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.output.rglob("*") if path.is_file()}
        return {"rows": rows, "files": files, "profile": self.store.profile("fixture")}

    def _record(self, document_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.documents).where(
                m.documents.c.id == document_id)).mappings().one())

    def _download(self, document_id, expected_text):
        result = self.client.get(f"/profiles/fixture/documents/{document_id}/txt")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn(expected_text, result.text)
        record = self._record(document_id)
        self.assertEqual(hashlib.sha256(result.content).hexdigest(), record["sha256"]["txt"])
        self.assertIn("no-store", result.headers["cache-control"])
        if os.name == "posix":
            path = self.output / self.application_id / record["files"]["txt"]
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        return record

    def test_matching_and_direct_export_reject_the_same_sensitive_paraphrases(self):
        self._save_answers([approved_answer(f"sensitive-{index}", question)
                            for index, question in enumerate(PARAPHRASES)])
        before = self._snapshot()
        for index, question in enumerate(PARAPHRASES):
            with self.subTest(question=question):
                match = self.client.post(self.base + "/answers/match", json={"question": question})
                self.assertEqual(match.status_code, 422, match.text)
                self.assertEqual(match.json()["detail"]["code"], "ANSWER_MATCH_SENSITIVE_QUESTION")
                suggestion = self.client.post(self.base + "/sensitive-answer-suggestion",
                                               json={"question": question})
                self.assertEqual(suggestion.status_code, 200, suggestion.text)
                self.assertEqual(suggestion.json()["response"], "")
                self.assertEqual(suggestion.json()["mode"], "ALWAYS_REQUIRE_REVIEW")
                exported = self._export(question, "SYNTHETIC_SAVED_ANSWER", f"sensitive-{index}")
                self.assertEqual(exported.status_code, 422, exported.text)
                self.assertNotIn("SYNTHETIC_SAVED_ANSWER", exported.text)
                self.assertEqual(self._snapshot(), before)

    def test_sensitive_saved_or_requested_wording_blocks_high_overlap_reuse(self):
        ordinary = "How many years of cloud service experience do you have?"
        sensitive = "How many years of military service experience do you have?"
        self._save_answers([approved_answer("ordinary", ordinary), approved_answer("sensitive", sensitive)])
        before = self._snapshot()
        for requested, answer_id in ((ordinary, "sensitive"), (sensitive, "ordinary")):
            with self.subTest(requested=requested, answer_id=answer_id):
                result = self._export(requested, "SYNTHETIC_SAVED_ANSWER", answer_id)
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(self._snapshot(), before)

    def test_explicit_policy_modes_do_not_authorize_sensitive_bank_id_reuse(self):
        question = PARAPHRASES[0]
        for policy, expected_response in (
            ({"mode": "ALWAYS_REQUIRE_REVIEW"}, ""),
            ({"mode": "PREFER_NOT_TO_ANSWER"}, "Prefer not to answer"),
            ({"mode": "USE_CONFIGURED_RESPONSE", "response": "SYNTHETIC_CONFIGURED_CHOICE"},
             "SYNTHETIC_CONFIGURED_CHOICE"),
        ):
            with self.subTest(mode=policy["mode"]):
                self._save_answers([approved_answer("military", question)], {"VETERAN_STATUS": policy})
                before = self._snapshot()
                suggestion = self.client.post(self.base + "/sensitive-answer-suggestion",
                                               json={"question": question})
                self.assertEqual(suggestion.status_code, 200, suggestion.text)
                self.assertEqual(suggestion.json()["response"], expected_response)
                self.assertEqual(suggestion.json()["mode"], policy["mode"])
                self.assertTrue(suggestion.json()["review_required"])
                result = self._export(question, "SYNTHETIC_SAVED_ANSWER", "military")
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(self._snapshot(), before)

    def test_ordinary_reuse_and_explicit_manual_sensitive_answers_remain_private_and_durable(self):
        ordinary = "Have you implemented military-grade encryption for stored data?"
        self._save_answers([approved_answer("ordinary", ordinary)], {
            "VETERAN_STATUS": {"mode": "USE_CONFIGURED_RESPONSE", "response": "SYNTHETIC_CONFIGURED_CHOICE"},
        })
        before = self._snapshot()
        match = self.client.post(self.base + "/answers/match", json={"question": ordinary})
        self.assertEqual(match.status_code, 200, match.text)
        self.assertEqual(match.json()["candidates"][0]["answer_id"], "ordinary")
        reused = self._export(ordinary, "SYNTHETIC_SAVED_ANSWER", "ordinary")
        self.assertEqual(reused.status_code, 201, reused.text)
        # The user deliberately selects a configured response for this application.
        manual = self._export(PARAPHRASES[0], "SYNTHETIC_CONFIGURED_CHOICE")
        self.assertEqual(manual.status_code, 201, manual.text)
        for result in (reused, manual):
            self.assertEqual(result.json()["external_submission"], "OFF")
        reused_id, manual_id = reused.json()["document_id"], manual.json()["document_id"]
        reused_record = self._download(reused_id, "SYNTHETIC_SAVED_ANSWER")
        manual_record = self._download(manual_id, "SYNTHETIC_CONFIGURED_CHOICE")
        reused_item = reused_record["generation_context"]["items"][0]
        self.assertEqual(reused_item["answer_mode"], "REVIEWED_REUSE")
        self.assertEqual(reused_item["answer_id"], "ordinary")
        self.assertEqual(reused_item["match_score"], 1.0)
        manual_item = manual_record["generation_context"]["items"][0]
        self.assertEqual(manual_item["answer_mode"], "USER_ENTERED")
        self.assertIsNone(manual_item["answer_id"])
        self.assertIsNone(manual_item["match_score"])
        self._reopen()
        self.assertEqual(self._download(reused_id, "SYNTHETIC_SAVED_ANSWER"), reused_record)
        self.assertEqual(self._download(manual_id, "SYNTHETIC_CONFIGURED_CHOICE"), manual_record)
        after = self._snapshot()
        self.assertEqual(after["profile"], before["profile"])
        for table in (m.applications, m.attempts, m.operations):
            self.assertEqual(after["rows"][table.name], before["rows"][table.name])
        for path, digest in before["files"].items():
            self.assertEqual(after["files"][path], digest)

    def test_review_ownership_and_exact_saved_text_guards_remain_required(self):
        question = "Which programming languages have you used?"
        self._save_answers([approved_answer("ordinary", question)])
        before = self._snapshot()
        for overrides, expected_status in (
            ({"reviewed": False}, 422),
            ({"profile_id": "other"}, 404),
            ({"answer_id": "foreign-answer"}, 422),
            ({"text": "CHANGED_SYNTHETIC_ANSWER"}, 422),
        ):
            with self.subTest(overrides=overrides):
                request = {"question": question, "text": "SYNTHETIC_SAVED_ANSWER", "answer_id": "ordinary"}
                request.update(overrides)
                result = self._export(**request)
                self.assertEqual(result.status_code, expected_status, result.text)
                self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
