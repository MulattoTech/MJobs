"""Real deterministic outputs retain exact public implementation provenance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.discovery.sources.collect import configure_source, collect_source
from services.documents.answers import export_answers
from services.documents.generate import prepare
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE


class PromptProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-prompt-provenance-")
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'provenance.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def reopen(self):
        self.store.engine.dispose()
        self.store = Store(self.url)

    def documents(self, application_id):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.documents).where(
                m.documents.c.application_id == application_id)).mappings()]

    def answer(self, application_id, **overrides):
        return export_answers(self.store, application_id, self.root, [SimpleNamespace(
            **{"question": "What working approach do you prefer?", "text": "Reviewed synthetic answer",
               "answer_id": None, "reviewed": True, **overrides})])

    def test_documents_and_reviewed_answers_survive_current_definition_change_and_reopen(self):
        app = prepare(self.store, self.job_id, "fixture", self.root, tailor_resume=True)
        answer = self.answer(app)
        old = self.documents(app)
        self.assertEqual(len(old), 3)
        # Assert the production gap before importing the new resolver.
        self.assertTrue(all("prompt_provenance" in row["generation_context"] for row in old))
        from packages.persistence.prompt_provenance import document_provenance
        from services.ai.prompt_provenance import capture_document
        from services.documents import cover_letter, templates
        identities = {row["id"]: document_provenance(self.store, "fixture", row["id"])
                      for row in old}
        old_files = {row["id"]: {extension: (self.root / app / relative).read_bytes()
                               for extension, relative in row["files"].items()} for row in old}
        self.assertIn("-v5", next(row["template"] for row in old if row["kind"] == "resume"))
        self.assertEqual(answer["template"], "reviewed-application-answers-v1")
        for identity in identities.values():
            self.assertFalse(identity["ai_executed"])
            self.assertEqual(identity["digest_kind"], "implementation-manifest-sha256")
            self.assertNotIn("Reviewed synthetic answer", json.dumps(identity))
            self.assertNotIn(PROFILE["name"], json.dumps(identity))
            self.assertNotIn(PROFILE["email"], json.dumps(identity))
        expected_answer = b"Question: What working approach do you prefer?\nAnswer: Reviewed synthetic answer\n"
        self.assertEqual((self.root / app / answer["files"]["txt"]).read_bytes(), expected_answer)
        # Same request remains idempotent, including immutable artifact bytes.
        self.assertEqual(prepare(self.store, self.job_id, "fixture", self.root,
                                 tailor_resume=True), app)
        self.assertEqual(len(self.documents(app)), 3)
        with patch.dict(cover_letter._OPENINGS, professional="Updated public opening."), \
                patch.dict(templates.HEADINGS, skill="Updated skills heading"):
            for row in old:
                target = {"resume": "resume", "cover-letter": "cover_letter",
                          "application-answer-export": "screening_answers"}[row["kind"]]
                current = capture_document(target, row["template"], row["generation_model"])
                if target != "screening_answers":
                    self.assertNotEqual(current["definition"]["sha256"],
                                        row["generation_context"]["prompt_provenance"]["definition"]["sha256"])
            self.reopen()
            for row in old:
                self.assertEqual(document_provenance(self.store, "fixture", row["id"]),
                                 identities[row["id"]])
                self.assertEqual({ext: (self.root / app / rel).read_bytes()
                                  for ext, rel in row["files"].items()}, old_files[row["id"]])
                with self.assertRaisesRegex(ValueError, "PROVENANCE_UNAVAILABLE"):
                    document_provenance(self.store, "other", row["id"])

    def test_actual_parser_keeps_historical_candidates_after_source_change_and_reopen(self):
        source = configure_source(self.store, "example")
        def transport(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": [RAW], "meta": {"total": 1}})
            return httpx.Response(200, json={"name": "Synthetic Employer"})
        collect_source(self.store, source, "fixture", transport=httpx.MockTransport(transport))
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.posting_value_candidates)).mappings().one()
            original = deepcopy(row["normalized"])
        self.assertIn("prompt_provenance", original["attributes"])
        from packages.persistence.prompt_provenance import parsing_provenance
        from services.discovery.sources.greenhouse import Greenhouse
        previous = parsing_provenance(self.store, row["id"])
        self.assertFalse(previous["ai_executed"])
        self.assertNotIn(RAW["content"], json.dumps(previous))
        normalize = Greenhouse.normalize
        def changed_normalize(adapter, raw):
            result = normalize(adapter, raw)
            result["title"] += " updated"
            return result
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source).values(
                last_attempt="2000-01-01T00:00:00+00:00"))
        with patch.object(Greenhouse, "normalize", changed_normalize):
            collect_source(self.store, source, "fixture", transport=httpx.MockTransport(transport))
        self.reopen()
        self.assertEqual(parsing_provenance(self.store, row["id"]), previous)
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.posting_value_candidates)).mappings().all()
        self.assertEqual(len(rows), 2)
        newer = next(candidate for candidate in rows if candidate["id"] != row["id"])
        self.assertNotEqual(parsing_provenance(self.store, newer["id"])["sha256"], previous["sha256"])
        self.assertEqual(original, next(candidate["normalized"] for candidate in rows
                                        if candidate["id"] == row["id"]))
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(
                m.company_sources.c.id == row["company_source_id"]).values(provider="lever"))
        with self.assertRaisesRegex(ValueError, "PROVENANCE_BINDING_MISMATCH"):
            parsing_provenance(self.store, row["id"])

    def test_resolver_rejects_tampering_malformed_versions_and_cross_target_bindings(self):
        app = prepare(self.store, self.job_id, "fixture", self.root)
        row = self.documents(app)[0]
        self.assertIn("prompt_provenance", row["generation_context"])
        from packages.persistence.prompt_provenance import document_provenance
        from services.ai.prompt_provenance import resolve_provenance, capture_document
        snapshot = row["generation_context"]["prompt_provenance"]
        for corrupted in (None, {}, [], {**snapshot, "unexpected": "value"}):
            with self.subTest(shape=type(corrupted).__name__), self.assertRaises(ValueError):
                resolve_provenance(corrupted)
        for field, value in (("sha256", "0" * 64), ("version", "9.9.9"),
                             ("target", "unknown"), ("template", "{}")):
            corrupted = deepcopy(snapshot)
            corrupted["definition"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                resolve_provenance(corrupted)
        with self.assertRaises(ValueError):
            capture_document("unknown", row["template"], row["generation_model"])
        with self.assertRaises(ValueError):
            capture_document("resume", "unknown-template", "unknown-model")
        context = deepcopy(row["generation_context"])
        context["prompt_provenance"] = capture_document(
            "screening_answers", "reviewed-application-answers-v1", "manual-reviewed-answers-v1")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(m.documents.c.id == row["id"]).values(
                generation_context=context))
        with self.assertRaises(ValueError):
            document_provenance(self.store, "fixture", row["id"])
        context = deepcopy(row["generation_context"])
        context["prompt_provenance"]["definition"]["sha256"] = "0" * 64
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(m.documents.c.id == row["id"]).values(
                generation_context=context))
        self.reopen()
        with self.assertRaises(ValueError):
            document_provenance(self.store, "fixture", row["id"])
        # Legacy records stay explicitly unrecorded, with no inferred defaults.
        context.pop("prompt_provenance")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(m.documents.c.id == row["id"]).values(
                generation_context=context))
        with self.assertRaisesRegex(ValueError, "PROVENANCE_UNRECORDED"):
            document_provenance(self.store, "fixture", row["id"])

    def test_denied_answers_and_unapproved_facts_do_not_gain_provenance_records(self):
        from apps.api.main import create_app
        app = prepare(self.store, self.job_id, "fixture", self.root)
        before = self.documents(app)
        # Review is enforced by the public request contract, not the internal exporter.
        with TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                   operation_mode="FIXTURE")) as client:
            denied = client.post(f"/profiles/fixture/applications/{app}/answers/export", json={
                "profile_id": "fixture", "items": [{"question": "Synthetic question?",
                    "text": "Synthetic answer", "reviewed": False}]})
            self.assertEqual(denied.status_code, 422)
        profile = deepcopy(PROFILE)
        profile["answers"] += [
            {"id": "sensitive", "question": "What is your disability?", "text": "Synthetic answer",
             "source": "synthetic review", "verified": True, "user_approved": True, "confidence": 100},
            {"id": "unapproved", "question": "Synthetic question?", "text": "Synthetic answer",
             "source": "synthetic review", "verified": False, "user_approved": False, "confidence": 100},
        ]
        self.store.save_profile(profile)
        for overrides, message in (
                ({"answer_id": "missing"}, "unavailable"),
                ({"question": "What is your disability?", "answer_id": "sensitive",
                  "text": "Synthetic answer"}, "Sensitive or legal"),
                ({"question": "Synthetic question?", "answer_id": "unapproved",
                  "text": "Synthetic answer"}, "verification and approval")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                self.answer(app, **overrides)
        self.assertEqual(self.documents(app), before)
        profile = deepcopy(PROFILE)
        profile["id"] = "unapproved"
        for fact in profile["facts"]:
            fact["verified"] = False
            fact["user_approved"] = False
        self.store.save_profile(profile)
        with self.assertRaises(ValueError):
            prepare(self.store, self.job_id, "unapproved", self.root)
