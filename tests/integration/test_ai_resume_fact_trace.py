"""Synthetic-provider acceptance for explicit AI wording and immutable fact trace."""
import hashlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.ai.providers import AIProviderError, ProviderCapabilities
from services.documents import ai_resume, tailor
from services.documents.errors import PreparationError
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration.test_spine import PROFILE

SOURCE = "Managed 40 Linux servers in 2020-2022."
PROPOSAL = "Maintained 40 Linux servers during 2020-2022."


class FixtureProvider:
    capabilities = ProviderCapabilities(text_generation=True, structured_output=True)

    def __init__(self):
        self.calls = []
        self.value = {"rewrites": [{"fact_id": "experience", "text": PROPOSAL}]}
        self.error = None
        self.callback = None

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        return self.value if isinstance(self.value, str) else json.dumps(self.value)


class AIResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-ai-resume-")
        self.root = Path(self.directory.name)
        self.output = self.root / "documents"
        self.url = f"sqlite:///{(self.root / 'test.sqlite').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile_data = {
            **PROFILE, "email": "private-contact-canary@example.test",
            "facts": [
                *PROFILE["facts"],
                self.fact("experience", SOURCE),
                self.fact("unselected", "Unselected confidential source canary."),
                {**self.fact("unapproved", "Unapproved secret source canary."),
                 "user_approved": False},
            ],
        }
        self.store.save_profile(self.profile_data)
        self.store.save_profile({**PROFILE, "id": "other-profile"})
        self.store.save_ai_task_settings("fixture", {
            "resume_tailoring": {"provider": "openai-compatible", "model": "fixture-model"},
        })
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "ai-resume-company", "name": "Synthetic Systems", "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": "ai-resume-job", "company_id": "ai-resume-company",
                "requisition": "AI-RESUME", "title": "Systems Engineer",
                "description": "Maintain Linux services.", "skills": ["Linux"],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "ai-resume-route", "job_id": "ai-resume-job",
                "company_id": "ai-resume-company", "provider": "fixture",
                "tenant": "ai-resume", "requisition": "AI-RESUME",
                "url": "https://jobs.example.test/ai-resume",
            })
        self.provider = FixtureProvider()
        self.request_number = 0

    @staticmethod
    def fact(identifier, text):
        return {
            "id": identifier, "text": text, "kind": "responsibility",
            "source": "synthetic-approved-record", "verified": True,
            "user_approved": True, "confidence": 100,
        }

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def request(self, **updates):
        self.request_number += 1
        args = {
            "request_id": f"fixture-request-{self.request_number:04d}",
            "selected_fact_ids": ["experience"],
            "expected_profile_version": self.store.profile("fixture")["version"],
            "expected_job_fingerprint": tailor.target_fingerprint(
                self.store.job("ai-resume-job", "fixture")),
            "consent": True,
            "provider_factory": lambda _name: self.provider,
        }
        args.update(updates)
        return ai_resume.request_draft(
            self.store, "fixture", "ai-resume-job", self.output, **args)

    def accept(self, draft, **updates):
        args = {
            "reviewed_sha256": draft["sha256"], "reviewed_fact_ids": ["experience"],
            "confirm_reviewed": True,
        }
        args.update(updates)
        return ai_resume.prepare_reviewed(
            self.store, "fixture", draft["draft_id"], self.output, **args)

    def assert_no_documents(self):
        self.assertEqual(self.store.count(m.documents.name), 0)
        self.assertEqual(self.store.count("applications"), 0)

    def test_changed_ai_wording_and_original_sources_survive_reopen(self):
        draft = self.request()
        self.assert_no_documents()
        review = ai_resume.review_draft(self.store, "fixture", draft["draft_id"], self.output)
        self.assertEqual(review["review"]["rewrites"][0]["source_text"], SOURCE)
        self.assertEqual(review["review"]["rewrites"][0]["proposed_text"], PROPOSAL)
        self.assertEqual(review["review"]["semantic_equivalence"], "USER_REVIEW_REQUIRED")
        accepted = self.accept(draft)
        self.store.engine.dispose()
        self.store = Store(self.url)
        documents = application_documents(self.store, accepted["application_id"], self.output)
        resume = next(row for row in documents if row["kind"] == "resume")
        letter = next(row for row in documents if row["kind"] == "cover-letter")
        self.assertEqual(resume["fact_ids"], ["experience"])
        self.assertEqual(resume["facts_used"][0]["text"], SOURCE)
        self.assertEqual(resume["facts_used"][0]["source"], "synthetic-approved-record")
        self.assertEqual(resume["missing_fact_ids"], [])
        self.assertIn(PROPOSAL, resume["preview_text"])
        self.assertIsNone(resume["preview_error"])
        self.assertEqual(set(resume["files"]), {"docx", "pdf", "txt"})
        for format, relative in resume["files"].items():
            content = (self.output / accepted["application_id"] / relative).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), resume["sha256"][format])
        trace = resume["generation_context"]["ai_proposal"]
        self.assertEqual(trace["source_fact_ids"], ["experience"])
        self.assertEqual(trace["provider"], "openai-compatible")
        self.assertEqual(trace["model"], "fixture-model")
        self.assertEqual(trace["draft_sha256"], draft["sha256"])
        self.assertFalse(resume["generation_context"]["prompt_provenance"]["ai_executed"])
        self.assertNotIn("ai_proposal", letter["generation_context"])
        self.assertEqual(len(self.provider.calls), 1)
        changed = [{**row, "text": "Changed current source."}
                   if row["id"] == "experience" else row for row in self.profile_data["facts"]]
        self.store.save_profile({**self.profile_data, "facts": changed})
        historical = next(row for row in application_documents(
            self.store, accepted["application_id"], self.output) if row["kind"] == "resume")
        self.assertEqual(historical["facts_used"][0]["text"], SOURCE)
        self.assertEqual(historical["generation_context"]["ai_proposal"], trace)
        self.assertIsNone(historical["preview_error"])

    def test_minimum_prompt_no_contact_unselected_unapproved_or_source_metadata(self):
        self.request()
        call = self.provider.calls[0]
        self.assertEqual(call["task"], "resume_tailoring")
        self.assertEqual(call["model"], "fixture-model")
        self.assertLessEqual(call["timeout_seconds"], 60)
        self.assertIsInstance(call["cancellation_event"], threading.Event)
        for forbidden in (
            "private-contact-canary", "Alex Fixture", "Unselected confidential",
            "Unapproved secret", "fictional-test-profile", "synthetic-approved-record",
        ):
            self.assertNotIn(forbidden, call["prompt"])
        self.assertIn(SOURCE, call["prompt"])
        with self.store.engine.connect() as connection:
            receipts = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action.like("AI_RESUME_%"))).scalars().all()
        serialized = json.dumps(receipts)
        self.assertNotIn(SOURCE, serialized)
        self.assertNotIn(PROPOSAL, serialized)

    def test_exact_request_and_acceptance_are_idempotent(self):
        first = self.request(request_id="same-explicit-request-123")
        second = self.request(request_id="same-explicit-request-123")
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertTrue(second["replayed"])
        one = self.accept(first)
        two = self.accept(first)
        self.assertEqual(one, two)
        self.assertEqual(self.store.count(m.documents.name), 2)
        self.assertEqual(len(self.provider.calls), 1)

    def test_attribution_changes_cannot_reuse_different_revision(self):
        kwargs = {
            "tailor_resume": True, "tailor_fact_ids": ["experience"],
            "tailor_fact_rewrites": [
                {"fact_id": "experience", "text": PROPOSAL, "reviewed": True},
            ],
        }
        application_id = prepare(self.store, "ai-resume-job", "fixture", self.output, **kwargs)
        self.assertEqual(self.store.application(application_id)["document_version"], 1)
        first = self.request()
        self.accept(first)
        self.assertEqual(self.store.application(application_id)["document_version"], 2)
        second = self.request()
        self.accept(second)
        self.assertEqual(self.store.application(application_id)["document_version"], 3)
        prepare(self.store, "ai-resume-job", "fixture", self.output, **kwargs)
        self.assertEqual(self.store.application(application_id)["document_version"], 4)
        resumes = [row for row in application_documents(
            self.store, application_id, self.output) if row["kind"] == "resume"]
        self.assertNotIn("ai_proposal", resumes[0]["generation_context"])
        self.assertEqual(resumes[1]["generation_context"]["ai_proposal"]["draft_id"],
                         second["draft_id"])
        self.assertEqual(resumes[2]["generation_context"]["ai_proposal"]["draft_id"],
                         first["draft_id"])

    def test_consent_and_complete_exact_review_are_mandatory(self):
        with self.assertRaises(PreparationError) as raised:
            self.request(consent=False)
        self.assertEqual(raised.exception.code, "AI_RESUME_CONSENT_REQUIRED")
        self.assertEqual(self.provider.calls, [])
        draft = self.request()
        for updates in (
            {"confirm_reviewed": False}, {"reviewed_fact_ids": []},
            {"reviewed_fact_ids": ["unselected"]}, {"reviewed_sha256": "0" * 64},
        ):
            with self.subTest(updates=updates):
                with self.assertRaises(PreparationError):
                    self.accept(draft, **updates)
                self.assert_no_documents()

    def test_unknown_duplicate_missing_and_extra_provider_fields_fail_closed(self):
        invalid = [
            {"rewrites": [{"fact_id": "unselected", "text": PROPOSAL}]},
            {"rewrites": [{"fact_id": "experience", "text": PROPOSAL}] * 2},
            {"rewrites": []},
            {"rewrites": [{"fact_id": "experience", "text": PROPOSAL, "reviewed": True}]},
            {"rewrites": [{"fact_id": "experience", "text": PROPOSAL}], "approved": True},
            '{"rewrites":[],"rewrites":[]}',
            "private-provider-canary-not-json",
            "x" * 100_001,
        ]
        for value in invalid:
            with self.subTest(kind=type(value).__name__):
                self.provider.value = value
                with self.assertRaises(PreparationError) as raised:
                    self.request()
                self.assertNotIn("private-provider-canary", str(raised.exception))
                self.assert_no_documents()

    def test_changed_protected_tokens_cannot_publish_a_draft(self):
        for text in ("Maintained 400 Linux servers during 2020-2022.",
                     "Maintained 40 Linux servers during 2021-2022.",
                     SOURCE):
            with self.subTest(text=text):
                self.provider.value = {"rewrites": [{"fact_id": "experience", "text": text}]}
                with self.assertRaises(PreparationError):
                    self.request()
                self.assert_no_documents()

    def test_unapproved_unsupported_and_duplicate_selection_refuse_before_egress(self):
        for identifiers in (["unapproved"], ["python"], ["experience", "experience"], []):
            with (self.subTest(identifiers=identifiers), self.assertRaises(PreparationError)):
                self.request(selected_fact_ids=identifiers)
        self.assertEqual(self.provider.calls, [])

    def test_profile_approval_and_job_changes_refuse_stale_proposals(self):
        draft = self.request()
        revoked = [{**row, "user_approved": False} if row["id"] == "experience" else row
                   for row in self.profile_data["facts"]]
        self.store.save_profile({**self.profile_data, "facts": revoked})
        with self.assertRaises(PreparationError):
            self.accept(draft)
        self.assert_no_documents()
        self.store.save_profile(self.profile_data)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == "ai-resume-job")
                               .values(description="Changed job after proposal."))
        with self.assertRaises(PreparationError):
            self.accept(draft)
        self.assert_no_documents()

    def test_profile_change_during_provider_call_refuses_result(self):
        self.provider.callback = lambda: self.store.save_profile({
            **self.profile_data, "name": "Changed during call",
        })
        with self.assertRaises(PreparationError) as raised:
            self.request()
        self.assertEqual(raised.exception.code, "AI_RESUME_PROFILE_CHANGED")
        self.assert_no_documents()

    def test_timeout_cancel_and_private_provider_failure_are_not_replayed(self):
        for error in (AIProviderError("AI_PROVIDER_TIMEOUT"),
                      AIProviderError("AI_PROVIDER_CANCELLED"),
                      RuntimeError("PRIVATE_API_KEY_AND_FACT_CANARY")):
            with self.subTest(error=type(error).__name__):
                self.provider.error = error
                key = f"uncertain-provider-{len(self.provider.calls):04d}"
                before = len(self.provider.calls)
                with self.assertRaises(PreparationError) as raised:
                    self.request(request_id=key)
                self.assertNotIn("PRIVATE_API_KEY", str(raised.exception))
                with self.assertRaises(PreparationError) as replay:
                    self.request(request_id=key)
                self.assertEqual(replay.exception.code, "AI_RESUME_CALL_NOT_REPLAYABLE")
                self.assertEqual(len(self.provider.calls), before + 1)
                self.assert_no_documents()

    def test_post_response_deadline_and_cancellation_refuse_publication(self):
        with (patch.object(ai_resume.time, "monotonic", side_effect=[0.0, 31.0]), self.assertRaises(PreparationError) as raised):
            self.request(timeout_seconds=30)
        self.assertEqual(raised.exception.code, "AI_RESUME_TIMEOUT")
        event = threading.Event()
        self.provider.callback = event.set
        with self.assertRaises(PreparationError) as raised:
            self.request(cancellation_event=event)
        self.assertEqual(raised.exception.code, "AI_RESUME_CANCELLED")
        self.assert_no_documents()

    def test_interrupted_call_receipt_prevents_another_provider_call(self):
        self.provider.callback = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.request(request_id="interrupted-call-12345")
        self.provider.callback = None
        with self.assertRaises(PreparationError) as raised:
            self.request(request_id="interrupted-call-12345")
        self.assertEqual(raised.exception.code, "AI_RESUME_CALL_NOT_REPLAYABLE")
        self.assertEqual(len(self.provider.calls), 1)

    def test_concurrent_same_request_has_only_one_provider_call(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            self.assertTrue(release.wait(5))
        self.provider.callback = blocked
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.request, request_id="concurrent-request-1234")
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(PreparationError):
                    self.request(request_id="concurrent-request-1234")
            finally:
                release.set()
            draft = pending.result(timeout=10)
        self.assertEqual(draft["state"], "REVIEW_REQUIRED")
        self.assertEqual(len(self.provider.calls), 1)

    def test_draft_file_tampering_and_cross_profile_access_refused(self):
        draft = self.request()
        with self.assertRaises(PreparationError) as raised:
            ai_resume.review_draft(self.store, "other-profile", draft["draft_id"], self.output)
        self.assertEqual(raised.exception.code, "AI_RESUME_DRAFT_NOT_FOUND")
        path = self.output / ".ai-resume-drafts" / f"{draft['draft_id']}.json"
        original = path.read_bytes()
        path.write_bytes(original.replace(b"Maintained", b"Fabricated"))
        with self.assertRaises(PreparationError):
            self.accept(draft)
        path.write_bytes(b"x" * (ai_resume.MAX_DRAFT_BYTES + 1))
        with self.assertRaises(PreparationError):
            ai_resume.review_draft(self.store, "fixture", draft["draft_id"], self.output)
        self.assert_no_documents()

    def test_disabled_saved_credentials_and_oversize_prompt_refuse_egress(self):
        self.store.save_ai_task_settings("fixture", {
            "resume_tailoring": {"provider": "disabled", "model": None},
        })
        with self.assertRaises(PreparationError) as raised:
            self.request()
        self.assertEqual(raised.exception.code, "AI_PROVIDER_DISABLED")
        self.store.save_ai_task_settings("fixture", {
            "resume_tailoring": {"provider": "openai", "model": "fixture-model"},
        })
        self.store.save_provider_credential("fixture", "openai", b"synthetic-ciphertext")
        with self.assertRaises(PreparationError) as raised:
            self.request()
        self.assertEqual(raised.exception.code, "AI_RESUME_SAVED_CREDENTIAL_UNSUPPORTED")
        facts = [self.fact(f"large-{n}", "Synthetic source " + "x" * 3_900) for n in range(10)]
        self.store.save_profile({**self.profile_data, "facts": facts})
        with self.assertRaises(PreparationError) as raised:
            self.request(selected_fact_ids=[row["id"] for row in facts])
        self.assertEqual(raised.exception.code, "AI_RESUME_PROMPT_TOO_LARGE")
        self.assertEqual(self.provider.calls, [])

    def test_cli_explicit_review_and_prepare_never_call_provider(self):
        from cli.ai_resume import main
        draft = self.request()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.dict("os.environ", {"MJOBS_DATABASE_URL": self.url}), redirect_stdout(stdout), redirect_stderr(stderr)):
            result = main(["review", "--profile-id", "fixture",
                           "--output-dir", str(self.output), "--draft-id", draft["draft_id"]])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["review"]["rewrites"][0]["source_text"], SOURCE)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.dict("os.environ", {"MJOBS_DATABASE_URL": self.url}), redirect_stdout(stdout), redirect_stderr(stderr)):
            result = main([
                "prepare", "--profile-id", "fixture", "--output-dir", str(self.output),
                "--draft-id", draft["draft_id"], "--reviewed-sha256", draft["sha256"],
                "--reviewed-fact-id", "experience", "--confirm-reviewed",
            ])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertFalse(json.loads(stdout.getvalue())["external_submission"])
        self.assertEqual(len(self.provider.calls), 1)

    def test_reordered_provider_rows_bind_to_original_fact_order(self):
        self.provider.value = {"rewrites": [
            {"fact_id": "unselected", "text": "Reviewed confidential source canary."},
            {"fact_id": "experience", "text": "  " + PROPOSAL + "  "},
        ]}
        draft = self.request(selected_fact_ids=["unselected", "experience"])
        review = ai_resume.review_draft(self.store, "fixture", draft["draft_id"], self.output)
        self.assertEqual(review["review"]["selected_fact_ids"], ["experience", "unselected"])
        accepted = self.accept(draft, reviewed_fact_ids=["experience", "unselected"])
        resume = next(row for row in application_documents(
            self.store, accepted["application_id"], self.output) if row["kind"] == "resume")
        self.assertIsNone(resume["preview_error"])
        self.assertEqual(resume["fact_ids"], ["experience", "unselected"])

    def test_history_remains_verifiable_without_draft_file_and_refuses_forged_trace(self):
        draft = self.request()
        accepted = self.accept(draft)
        (self.output / ".ai-resume-drafts" / f"{draft['draft_id']}.json").unlink()
        resume = next(row for row in application_documents(
            self.store, accepted["application_id"], self.output) if row["kind"] == "resume")
        self.assertIsNone(resume["preview_error"])
        context = json.loads(json.dumps(resume["generation_context"]))
        context["ai_proposal"]["model"] = "unexecuted-model"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(m.documents.c.id == resume["id"])
                               .values(generation_context=context))
        tampered = next(row for row in application_documents(
            self.store, accepted["application_id"], self.output) if row["kind"] == "resume")
        self.assertIsNone(tampered["preview_text"])
        self.assertIsNotNone(tampered["preview_error"])

    def test_real_loopback_provider_and_cli_contract(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from cli.ai_resume import main
        recorded = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                recorded.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": json.dumps({
                        "rewrites": [{"fact_id": "experience", "text": PROPOSAL}],
                    })}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.store.save_ai_task_settings("fixture", {
            "resume_tailoring": {"provider": "local", "model": "fixture-model"},
        })
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with (patch.dict("os.environ", {
                "MJOBS_DATABASE_URL": self.url,
                "MJOBS_LOCAL_AI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "MJOBS_LOCAL_AI_API_KEY": "",
            }), redirect_stdout(stdout), redirect_stderr(stderr)):
                result = main([
                    "draft", "--profile-id", "fixture", "--job-id", "ai-resume-job",
                    "--output-dir", str(self.output), "--request-id", "real-loopback-12345",
                    "--fact-id", "experience",
                    "--expected-profile-version", self.store.profile("fixture")["version"],
                    "--expected-job-fingerprint", tailor.target_fingerprint(
                        self.store.job("ai-resume-job", "fixture")),
                    "--consent-send-selected-facts",
                ])
            self.assertEqual(result, 0, stderr.getvalue())
            draft = json.loads(stdout.getvalue())
            accepted = self.accept(draft)
            resume = next(row for row in application_documents(
                self.store, accepted["application_id"], self.output) if row["kind"] == "resume")
            self.assertEqual(resume["generation_context"]["ai_proposal"]["provider"], "local")
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["model"], "fixture-model")
            self.assertIn("response_format", recorded[0])
            self.assertIsNone(resume["preview_error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())


    def test_reacceptance_reports_preserved_application_state(self):
        draft = self.request()
        first = self.accept(draft)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == first["application_id"]).values(state="DRY_RUN_COMPLETE"))
        repeated = self.accept(draft)
        self.assertEqual(repeated["state"], "DRY_RUN_COMPLETE")
        self.assertEqual(repeated["document_version"], 1)
        self.assertEqual(self.store.application(first["application_id"])["state"], "DRY_RUN_COMPLETE")
        self.assertEqual(len(self.provider.calls), 1)

    def test_old_prompt_version_remains_readable_when_generation_version_changes(self):
        draft = self.request()
        accepted = self.accept(draft)
        with patch.object(ai_resume, "PROMPT_VERSION", "future-generation-contract"):
            resume = next(row for row in application_documents(
                self.store, accepted["application_id"], self.output) if row["kind"] == "resume")
            self.assertIsNone(resume["preview_error"])
            self.assertEqual(resume["generation_context"]["ai_proposal"]["prompt_version"],
                             "resume-fact-rewrites-v1")
            self.assertEqual(ai_resume.review_draft(
                self.store, "fixture", draft["draft_id"], self.output)["state"], "REVIEW_REQUIRED")



if __name__ == "__main__":
    unittest.main()
