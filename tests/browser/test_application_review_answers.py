"""Connected local-browser acceptance for manually reviewed application answers."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.preflight import latest_preflight, persist_preflight
from services.applications.mock_adapter import apply_mock
from services.applications.attempt_history import read_attempt_history
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]
QUESTION_ID = "fixture_question"
QUESTION = "Approved fixture answer"
SENSITIVE_QUESTION_ID = "legal-authorization-review"
SENSITIVE_QUESTION = "Are you legally authorized to work in the United States?"
MANUAL_ANSWER = "Controlled fictional test application."
CONCURRENT_ANSWER_ID = "concurrent-version-change"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationReviewAnswersBrowserTests(unittest.TestCase):
    def test_reviewed_answer_requires_persisted_save_and_isolated_readback(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-reviewed-answer-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = deepcopy(PROFILE)
            # Keep only already-approved synthetic ATS answers so the resumed
            # revision can pass the fixture form after the required answer is
            # explicitly reviewed and saved with a new durable identity.
            profile["answers"] = [
                {"id": "synthetic-country", "question": "Country",
                 "text": "United States", "source": "User-reviewed fixture",
                 "verified": True, "user_approved": True, "confidence": 95},
                {"id": "synthetic-remote", "question": "Remote role",
                 "text": "yes", "source": "User-reviewed fixture",
                 "verified": True, "user_approved": True, "confidence": 95},
                {"id": "synthetic-fixture-answer", "question": "Approved fixture answer",
                 "text": "Controlled fictional test application.",
                 "source": "User-reviewed fixture", "verified": True,
                 "user_approved": True, "confidence": 95},
            ]
            store.save_profile(profile)
            other_profile = deepcopy(PROFILE)
            other_profile.update(id="other-profile", name="Other synthetic applicant",
                                 email="other@example.invalid", answers=[])
            store.save_profile(other_profile)

            # The fixture server is loopback-only and serves synthetic postings; the
            # browser below blocks every origin other than its isolated local API.
            fixture_context = fixture_server()
            fixture = fixture_context.__enter__()
            self.addCleanup(fixture_context.__exit__, None, None, None)
            discover(store, fixture, "fixture")
            discover(store, fixture, "other-profile")
            job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
            other_job = next(row for row in store.jobs("other-profile")
                             if row["requisition"] == "B")
            self.assertEqual(job["id"], other_job["id"])
            application_ids = {
                "fixture": prepare(store, job["id"], "fixture", root,
                                    tailor_resume=True, include_resume_contact=True,
                                    cover_letter_style="warm", cover_letter_length="detailed"),
                "other-profile": prepare(store, other_job["id"], "other-profile", root,
                                          tailor_resume=True, include_resume_contact=True,
                                          cover_letter_style="warm", cover_letter_length="detailed"),
            }
            # A later verified employer route must not silently retarget a saved
            # application when Resume reuses its DEFAULT preparation settings.
            with store.engine.begin() as connection:
                connection.execute(m.company_domains.insert().values(
                    id="browser-verified-employer-domain", company_id=job["company_id"],
                    domain="browser-verified-employer.example", confidence=1.0,
                    verified=True, verification_method="USER_REVIEWED_FIXTURE",
                    first_seen_at="2026-10-18T00:00:00Z",
                    last_verified_at="2026-10-18T00:00:00Z",
                    evidence=["synthetic reviewed employer identity"],
                ))
                connection.execute(m.routes.insert().values(
                    id="browser-new-higher-authority-route", job_id=job["id"],
                    company_id=job["company_id"], provider="employer", tenant="fixture",
                    requisition=job["requisition"],
                    url="https://browser-verified-employer.example/apply",
                ))
            for profile_id, application_id in application_ids.items():
                application = store.application(application_id)
                persist_preflight(store, application_id, {
                    "result": "FAIL",
                    "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
                    "observed_at": "2026-10-09T00:00:00Z",
                    "profile_version": application["profile_version"],
                    "document_version": application["document_version"],
                    "route_id": application["route_id"],
                    "provider": "mock-ats",
                    "tenant": "example",
                    "requisition": "B",
                    "metadata_fingerprint": "b" * 64,
                    "mappings": [],
                    "missing": [
                        {"question_id": QUESTION_ID, "question": QUESTION,
                         "fact_ids": [], "answer_ids": [], "profile_paths": [],
                         "document_kinds": []},
                        {"question_id": SENSITIVE_QUESTION_ID,
                         "question": SENSITIVE_QUESTION,
                         "fact_ids": [], "answer_ids": [], "profile_paths": [],
                         "document_kinds": []},
                    ],
                })

            # Begin from a real completed local dry run on the old pins. Keep its
            # attempt history, then restore the unresolved-answer review fixture
            # for the UX action below.
            with ThreadPoolExecutor(max_workers=1) as executor:
                old_run = executor.submit(
                    apply_mock, store, application_ids["fixture"], fixture, root,
                    request_id="old-revision-completed-dryrun",
                ).result(timeout=45)
            self.assertEqual(old_run["state"], "DRY_RUN_COMPLETE")
            old_attempt = read_attempt_history(
                store, "fixture", application_ids["fixture"],
            )["attempts"][0]
            self.assertEqual(old_attempt["state"], "DRY_RUN_COMPLETE")
            self.assertEqual(old_attempt["document_version"],
                             store.application(application_ids["fixture"])["document_version"])
            old_preflight = latest_preflight(store, application_ids["fixture"])
            self.assertEqual(old_preflight["result"], "PASS")
            old_application = store.application(application_ids["fixture"])
            persist_preflight(store, application_ids["fixture"], {
                "result": "FAIL", "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
                "observed_at": "2026-10-18T00:01:00Z",
                "profile_version": old_application["profile_version"],
                "document_version": old_application["document_version"],
                "route_id": old_application["route_id"], "provider": "mock-ats",
                "tenant": "example", "requisition": "B",
                "metadata_fingerprint": old_preflight["metadata_fingerprint"],
                "mappings": [], "missing": [
                    {"question_id": QUESTION_ID, "question": QUESTION,
                     "fact_ids": [], "answer_ids": [], "profile_paths": [],
                     "document_kinds": []},
                    {"question_id": SENSITIVE_QUESTION_ID,
                     "question": SENSITIVE_QUESTION,
                     "fact_ids": [], "answer_ids": [], "profile_paths": [],
                     "document_kinds": []},
                ],
            })

            applications_before = [store.application(identifier)
                                   for identifier in application_ids.values()]
            documents_before = store.count("documents")
            attempts_before = store.count("application_attempts")
            app = create_app(store, fixture, root, operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                app, log_level="error", access_log=False))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            external, errors, writes = [], [], []
            request_bodies = []
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application review API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 950})

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def record_write(request):
                        if request.method not in {"GET", "HEAD", "OPTIONS"}:
                            writes.append((request.method, urlsplit(request.url).path))
                            if request.post_data:
                                try:
                                    request_bodies.append(json.loads(request.post_data))
                                except (TypeError, json.JSONDecodeError):
                                    pass

                    page.on("request", record_write)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value(
                        "fixture")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    queue = page.get_by_role("region", name="Application review queue")
                    expect(queue).to_contain_text(job["company"])
                    queue.get_by_role("button", name=(
                        f"Review application: {job['title']} at {job['company']}"
                    )).click()

                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    question_row = unresolved.locator("li").filter(has_text=QUESTION)
                    question_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role(
                            "group", name="Question 1")
                    expect(editor.get_by_role("textbox", name="Employer question", exact=True)).to_have_value(
                        QUESTION)
                    answer = editor.get_by_role("textbox", name="Answer", exact=True)
                    expect(answer).to_have_value("")
                    expect(editor).not_to_contain_text("Suggested answer")
                    expect(editor).not_to_contain_text("Confidence")
                    bank_button = editor.get_by_role(
                        "button", name="Save to Answer Bank", exact=True)
                    expect(bank_button).to_be_disabled()

                    # Editing and reviewing text remains local until the explicit save.
                    profile_before_typing = deepcopy(store.profile("fixture"))
                    answer.fill(MANUAL_ANSWER)
                    editor.get_by_label("I reviewed this exact answer for this job", exact=True).check()
                    self.assertEqual(writes, [],
                                     "Typing and review acknowledgement must not persist")
                    self.assertEqual(store.profile("fixture"), profile_before_typing)
                    expect(bank_button).to_be_enabled()
                    bank_button.click()
                    confirmation = editor.locator('[aria-label="Answer Bank confirmation for question 1"]')
                    confirm_save = confirmation.get_by_role(
                        "button", name="Confirm and save to Answer Bank", exact=True)
                    expect(confirm_save).to_be_disabled()
                    confirmation.get_by_label(
                        "I confirm this answer is accurate and may be reused", exact=True).check()
                    expect(confirm_save).to_be_enabled()
                    self.assertEqual(writes, [], "Confirmation is local until explicit save")

                    # Change the profile after the save reads its expected version.
                    # The first save must be rejected without persisting this answer.
                    def concurrent_profile_change(route):
                        if (route.request.method == "PUT"
                                and urlsplit(route.request.url).path == "/profiles"):
                            current = store.profile("fixture")
                            store.save_profile({**current, "answers": [
                                *current["answers"], {
                                    "id": CONCURRENT_ANSWER_ID,
                                    "question": "A concurrent synthetic question?",
                                    "text": "Unrelated concurrent profile edit.",
                                    "source": "Synthetic concurrent edit",
                                    "confidence": 90,
                                    "verified": True,
                                    "user_approved": True,
                                },
                            ]})
                        route.continue_()

                    context.route("**/profiles", concurrent_profile_change)
                    with page.expect_response(lambda response:
                            response.request.method == "PUT"
                            and urlsplit(response.url).path == "/profiles") as stale_save:
                        confirm_save.click()
                    self.assertEqual(stale_save.value.status, 409)
                    expect(page.locator('main > .alert[role="alert"]')).to_contain_text(
                        "Profile changed while this answer was being saved")
                    answers_after_rejection = store.profile("fixture")["answers"]
                    question_answers = [item for item in answers_after_rejection
                                        if item.get("question") == QUESTION]
                    self.assertEqual(
                        [item["id"] for item in question_answers],
                        ["synthetic-fixture-answer"],
                        "The rejected save must not replace the durable answer identity",
                    )
                    expect(confirmation).to_be_visible()
                    context.unroute("**/profiles", concurrent_profile_change)

                    # Reload obtains the current profile version. Saving now must
                    # first expose the stale application revision explicitly. The
                    # old preflight must not put that application back in the review
                    # queue until the user resumes it and a new preflight is created.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Applications", exact=False).click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    pipeline.locator(".job-card").filter(has_text=job["title"]).click()
                    stale_notice = page.get_by_role("region", name="ATS question preflight")
                    expect(stale_notice).to_contain_text(
                        "older profile or document revision")
                    expect(page.get_by_role("region", name="Application review queue")).to_have_count(0)
                    page.get_by_role("button", name="Resume and prepare current revision").click()
                    expect(page.get_by_text("Current application documents are revision", exact=False)).to_contain_text(
                        "Run a local dry run to create a fresh ATS question preflight")
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        refreshed_run = executor.submit(
                            apply_mock, store, application_ids["fixture"], fixture, root,
                            request_id="resume-stale-preflight",
                        ).result(timeout=45)
                    self.assertEqual(refreshed_run["state"], "DRY_RUN_COMPLETE")
                    resumed_preflight = latest_preflight(
                        store, application_ids["fixture"])
                    resumed_application = store.application(application_ids["fixture"])
                    # Bind the real required fixture question to the resumed pins
                    # after the current dry run so only current unresolved
                    # metadata can reopen question review.
                    persist_preflight(store, application_ids["fixture"], {
                        "result": "FAIL", "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
                        "observed_at": "2026-10-18T00:02:00Z",
                        "profile_version": resumed_application["profile_version"],
                        "document_version": resumed_application["document_version"],
                        "route_id": resumed_application["route_id"], "provider": "mock-ats",
                        "tenant": "example", "requisition": "B",
                        "metadata_fingerprint": resumed_preflight["metadata_fingerprint"], "mappings": [], "missing": [
                            {"question_id": QUESTION_ID, "question": QUESTION,
                             "fact_ids": [], "answer_ids": [], "profile_paths": [],
                             "document_kinds": []},
                        ],
                    })
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    queue = page.get_by_role("region", name="Application review queue")
                    queue.get_by_role("button", name=(
                        f"Review application: {job['title']} at {job['company']}"
                    )).click()
                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    question_row = unresolved.locator("li").filter(has_text=QUESTION)
                    question_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role(
                            "group", name="Question 1")
                    answer = editor.get_by_role("textbox", name="Answer", exact=True)
                    answer.fill(MANUAL_ANSWER)
                    editor.get_by_label("I reviewed this exact answer for this job", exact=True).check()
                    editor.get_by_role("button", name="Save to Answer Bank", exact=True).click()
                    confirmation = editor.locator('[aria-label="Answer Bank confirmation for question 1"]')
                    confirm_save = confirmation.get_by_role(
                        "button", name="Confirm and save to Answer Bank", exact=True)
                    expect(confirm_save).to_be_disabled()
                    confirmation.get_by_label(
                        "I confirm this answer is accurate and may be reused", exact=True).check()
                    with page.expect_response(lambda response:
                            response.request.method == "PUT"
                            and urlsplit(response.url).path == "/profiles") as saved_response:
                        with page.expect_response(lambda response:
                                response.request.method == "GET"
                                and urlsplit(response.url).path == "/profiles/fixture"
                                and MANUAL_ANSWER in response.text()) as saved_readback:
                            confirm_save.click()
                    self.assertEqual(saved_response.value.status, 200)
                    payload = saved_response.value.json()
                    self.assertEqual(payload["id"], "fixture")
                    self.assertTrue(payload["version"])
                    saved_answer_profile = saved_readback.value.json()
                    self.assertEqual(saved_answer_profile["id"], "fixture")
                    persisted_response = next(item for item in saved_answer_profile["answers"]
                                              if item.get("question") == QUESTION)
                    self.assertEqual(persisted_response["text"], MANUAL_ANSWER)
                    self.assertTrue(persisted_response["verified"])
                    self.assertTrue(persisted_response["user_approved"])
                    saved_profile = store.profile("fixture")
                    persisted = next(item for item in saved_profile["answers"]
                                     if item.get("question") == QUESTION)
                    self.assertEqual(persisted["text"], MANUAL_ANSWER)
                    self.assertTrue(persisted["user_approved"])
                    self.assertTrue(persisted["verified"])
                    self.assertTrue(any(item.get("id") == CONCURRENT_ANSWER_ID
                                        for item in saved_profile["answers"]),
                                    "Retry must preserve the concurrent profile edit")
                    self.assertNotEqual(saved_profile["version"],
                                        applications_before[0]["profile_version"])
                    expect(page.get_by_role("status").filter(has_text="Reviewed answer saved")).to_contain_text(
                        "Reviewed answer saved to this profile's Answer Bank")
                    expect(confirm_save).to_be_disabled()
                    expect(editor).to_contain_text("Selected draft only")
                    self.assertEqual(sum(method == "PUT" and path == "/profiles"
                                         for method, path in writes), 2,
                                     "Only the rejected save and explicit confirmed save may update the profile")
                    self.assertEqual(sum(method == "POST" and path.endswith("/reprepare")
                                         for method, path in writes), 1,
                                     "The browser must record exactly one explicit reprepare")

                    # A reviewed answer changes the profile version, so the old
                    # application snapshot needs an explicit, audited resume.
                    resume = page.get_by_role(
                        "region", name="Resume application preparation")
                    expect(resume).to_contain_text("current approved facts and answers")
                    with store.engine.begin() as connection:
                        connection.execute(m.operations.insert().values(
                            id="queued-review-resume-prepare", kind="PREPARE",
                            entity_id="fixture",
                            params={"job_id": job["id"],
                                    "profile_version": saved_profile["version"],
                                    "request_id": "queued-review-resume-prepare"},
                            state="QUEUED", updated_at="2026-10-18T00:00:00Z",
                        ))
                    old_document_version = store.application(
                        application_ids["fixture"])["document_version"]
                    with page.expect_response(lambda response:
                            response.request.method == "POST"
                            and urlsplit(response.url).path.endswith("/reprepare")) as blocked:
                        resume.get_by_role(
                            "button", name="Resume and prepare current revision").click()
                    self.assertEqual(blocked.value.status, 409, blocked.value.text())
                    expect(page.get_by_role("alert").filter(
                        has_text="An application action is still in progress")).to_be_visible()
                    self.assertEqual(store.application(application_ids["fixture"])["document_version"],
                                     old_document_version)
                    with store.engine.begin() as connection:
                        connection.execute(m.operations.update().where(
                            m.operations.c.id == "queued-review-resume-prepare"
                        ).values(state="CANCELLED"))
                    with page.expect_response(lambda response:
                            response.request.method == "POST"
                            and urlsplit(response.url).path.endswith("/reprepare")) as reprepare:
                        resume.get_by_role(
                            "button", name="Resume and prepare current revision").click()
                    self.assertEqual(reprepare.value.status, 200, reprepare.value.text())
                    prepared = reprepare.value.json()
                    self.assertEqual(prepared["id"], application_ids["fixture"])
                    self.assertEqual(prepared["route_id"], applications_before[0]["route_id"])
                    self.assertEqual(prepared["profile_version"], saved_profile["version"])
                    self.assertEqual(prepared["document_version"],
                                     old_document_version + 1)
                    expect(page.get_by_role("status").filter(has_text=
                        "Run a local dry run to create a fresh ATS question preflight")).to_be_visible()
                    refreshed_documents = page.request.get(
                        f"{origin}/profiles/fixture/applications/"
                        f"{application_ids['fixture']}/documents")
                    self.assertEqual(refreshed_documents.status, 200)
                    revisions = refreshed_documents.json()["documents"]
                    self.assertTrue(any(row["version"] == applications_before[0]["document_version"]
                                        and row["profile_version"] == applications_before[0]["profile_version"]
                                        for row in revisions))
                    self.assertTrue(any(row["version"] == prepared["document_version"]
                                        and row["profile_version"] == saved_profile["version"]
                                        for row in revisions))
                    revised_documents = [row for row in revisions
                                         if row["version"] == prepared["document_version"]]
                    revised_resume = next(row for row in revised_documents
                                          if row["kind"] == "resume")
                    revised_letter = next(row for row in revised_documents
                                          if row["kind"] == "cover-letter")
                    self.assertIn("contact_header", revised_resume["generation_context"])
                    self.assertEqual(revised_letter["generation_context"]["style"], "warm")
                    self.assertEqual(revised_letter["generation_context"]["length"], "detailed")
                    durable = store.application(application_ids["fixture"])
                    self.assertEqual(durable["document_version"], prepared["document_version"])
                    self.assertEqual(durable["profile_version"], saved_profile["version"])
                    with store.engine.connect() as connection:
                        audit_rows = list(connection.execute(select(m.audit).where(
                            m.audit.c.entity_id == application_ids["fixture"],
                            m.audit.c.action == "DOCUMENTS_PREPARED",
                        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())).mappings())
                    reprepare_event = next(row for row in audit_rows
                                           if row["payload"].get("reprepare"))
                    self.assertEqual(reprepare_event["payload"]["reprepare"]["prior"]["document_version"],
                                     old_document_version)
                    self.assertFalse(prepared["question_preflight"]["is_current"],
                                     "The old preflight must remain historical for current documents")
                    refreshed_answer = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role(
                            "group", name="Question 1")
                    expect(refreshed_answer.get_by_role(
                        "textbox", name="Answer", exact=True)).to_have_value(MANUAL_ANSWER)
                    expect(refreshed_answer.get_by_label(
                        "I reviewed this exact answer for this job", exact=True)).not_to_be_checked()

                    # The profile-scoped read route proves the exact approved text
                    # survived the durable save independently of React state.
                    readback = page.request.get(
                        f"{origin}/profiles/fixture/applications/"
                        f"{application_ids['fixture']}/answers")
                    self.assertEqual(readback.status, 200)
                    readback_answers = readback.json()["answers"]
                    self.assertTrue(any(
                        item.get("question") == QUESTION
                        and item.get("text") == MANUAL_ANSWER
                        for item in readback_answers), readback_answers)
                    settings = page.request.get(f"{origin}/settings")
                    self.assertFalse(settings.json()["external_submission_enabled"])

                    # Admit the exact saved answer through the public resume API,
                    # execute its durable operation through the real worker, and
                    # let the UI continue only after the exact PASS readback.
                    resume_validation = refreshed_answer.locator(
                        '[aria-label="Resume validation for question 1"]')
                    resume_button = resume_validation.get_by_role(
                        "button", name="Resume and validate current form", exact=True)
                    expect(resume_button).to_be_enabled()
                    with page.expect_response(lambda response:
                            response.request.method == "POST"
                            and urlsplit(response.url).path.endswith(
                                "/answer-review-resumes")) as admitted:
                        resume_button.click()
                    self.assertEqual(admitted.value.status, 202, admitted.value.text())
                    operation_id = admitted.value.json()["operation_id"]
                    environment = {
                        "MJOBS_DATABASE_URL": store.url,
                        "MJOBS_OUTPUT_DIR": str(root),
                        "MJOBS_MOCK_ORIGIN": fixture,
                        "MJOBS_OPERATION_MODE": "FIXTURE",
                    }
                    with patch.dict(os.environ, environment), \
                            ThreadPoolExecutor(max_workers=1) as executor:
                        worker_result = executor.submit(
                            execute_operation, operation_id,
                        ).result(timeout=45)
                    self.assertEqual(worker_result["status"], "COMPLETED", worker_result)
                    expect(page.get_by_role("status").filter(has_text=
                        "Fresh application validation passed")).to_be_visible(timeout=15000)
                    self.assertIn(("POST", urlsplit(admitted.value.url).path), writes)
                    completed = page.request.get(
                        f"{origin}/profiles/fixture/applications/"
                        f"{application_ids['fixture']}")
                    self.assertEqual(completed.status, 200)
                    completed_app = completed.json()
                    self.assertEqual(completed_app["state"], "DRY_RUN_COMPLETE")
                    self.assertEqual(completed_app["document_version"],
                                     prepared["document_version"])
                    self.assertEqual(completed_app["question_preflight"]["document_version"],
                                     prepared["document_version"])
                    self.assertEqual(completed_app["question_preflight"]["profile_version"],
                                     saved_profile["version"])
                    self.assertFalse(completed_app["question_preflight"]["missing"])
                    history_response = page.request.get(
                        f"{origin}/profiles/fixture/applications/"
                        f"{application_ids['fixture']}/attempt-history")
                    self.assertEqual(history_response.status, 200)
                    history = history_response.json()
                    self.assertFalse(history["external_submission_enabled"])
                    self.assertEqual(len(history["attempts"]), 3)
                    self.assertTrue(any(
                        row["document_version"] == applications_before[0]["document_version"]
                        and row["state"] == "DRY_RUN_COMPLETE"
                        for row in history["attempts"]))
                    self.assertTrue(any(
                        row["document_version"] == prepared["document_version"]
                        and row["profile_version"] == saved_profile["version"]
                        and row["state"] == "DRY_RUN_COMPLETE"
                        for row in history["attempts"]))
                    page.get_by_role("button", name="Applications", exact=False).click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    pipeline.get_by_role("button", name=job["title"], exact=False).click()
                    expect(page.get_by_role(
                        "status", name="Application state", exact=True)).to_have_text(
                            "DRY_RUN_COMPLETE")
                    history_panel = page.get_by_role(
                        "region", name="Durable attempt history", exact=True)
                    with page.expect_response(lambda response:
                            response.request.method == "GET"
                            and urlsplit(response.url).path.endswith(
                                "/attempt-history")) as refreshed_history:
                        history_panel.get_by_role(
                            "button", name="Refresh attempt history", exact=True).click()
                    self.assertEqual(refreshed_history.value.status, 200)
                    for attempt in history["attempts"]:
                        expect(history_panel).to_contain_text(attempt["attempt_id"])
                    expect(history_panel).to_contain_text("Frozen inputs: profile version")

                    # A different profile sees no answer from profile A.
                    page.get_by_label("Active profile", exact=True).select_option(
                        "other-profile")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    other_queue = page.get_by_role(
                        "region", name="Application review queue")
                    other_queue.get_by_role("button", name=(
                        f"Review application: {other_job['title']} at {other_job['company']}"
                    )).click()
                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    question_row = unresolved.locator("li").filter(has_text=QUESTION)
                    question_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    other_editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role("group", name="Question 1")
                    expect(other_editor.get_by_role("textbox", name="Answer", exact=True)).to_have_value("")
                    self.assertFalse(any(item.get("text") == MANUAL_ANSWER
                                         for item in store.profile("other-profile")["answers"]))

                    # Sensitive prompts cannot be promoted into the reusable bank.
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    other_queue = page.get_by_role(
                        "region", name="Application review queue")
                    other_queue.get_by_role("button", name=(
                        f"Review application: {other_job['title']} at {other_job['company']}"
                    )).click()
                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    sensitive_row = unresolved.locator("li").filter(
                        has_text=SENSITIVE_QUESTION)
                    sensitive_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    # Reselecting this same application preserves the previous
                    # local question; the sensitive prompt is a separate draft.
                    expect(other_editor.get_by_role(
                        "textbox", name="Employer question", exact=True)).to_have_value(QUESTION)
                    sensitive_editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role("group", name="Question 2", exact=True)
                    expect(sensitive_editor.get_by_role(
                        "textbox", name="Employer question", exact=True)).to_have_value(SENSITIVE_QUESTION)
                    expect(sensitive_editor).to_contain_text(
                        "Sensitive or legal question: enter your answer for this application.")
                    sensitive_editor.get_by_role("textbox", name="Answer", exact=True).fill("Synthetic application-only answer")
                    sensitive_editor.get_by_label(
                        "I reviewed this exact answer for this job", exact=True).check()
                    expect(sensitive_editor.get_by_role(
                        "button", name="Save to Answer Bank", exact=True)).to_have_count(0)
                    expect(sensitive_editor.get_by_role(
                        "button", name="Confirm and save to Answer Bank", exact=True)).to_have_count(0)

                    self.assertEqual(writes, [
                        ("PUT", "/profiles"),
                        ("POST", f"/profiles/fixture/applications/{application_ids['fixture']}/reprepare"),
                        ("PUT", "/profiles"),
                        ("POST", f"/profiles/fixture/applications/{application_ids['fixture']}/reprepare"),
                        ("POST", f"/profiles/fixture/applications/{application_ids['fixture']}/reprepare"),
                        ("POST", f"/profiles/fixture/applications/{application_ids['fixture']}/answer-review-resumes"),
                    ])
                    self.assertEqual(len(request_bodies), 6)
                    for profile_id in ("fixture", "other-profile"):
                        self.assertFalse(any(
                            item.get("text") == "Synthetic application-only answer"
                            for item in store.profile(profile_id)["answers"]))
                    self.assertTrue(any(item.get("id") == CONCURRENT_ANSWER_ID
                                        for item in request_bodies[2]["answers"]))
                    for body in (request_bodies[0], request_bodies[2]):
                        self.assertEqual(body["id"], "fixture")
                        self.assertTrue(body["expected_version"])
                        self.assertFalse(any(
                            item.get("text") == "Synthetic application-only answer"
                            for item in body["answers"]))
                        self.assertTrue(any(
                            item.get("question") == QUESTION
                            and item.get("text") == MANUAL_ANSWER
                            for item in body["answers"]))
                    self.assertEqual(set(request_bodies[3]), {
                        "expected_document_version", "expected_application_profile_version",
                        "expected_profile_version", "expected_state",
                    })
                    self.assertEqual(request_bodies[3], request_bodies[4])
                    self.assertEqual(set(request_bodies[5]), {
                        "request_id", "question_id", "question_fingerprint",
                        "form_fingerprint", "answer_id", "expected_profile_version",
                    })
                    self.assertEqual(request_bodies[5]["question_id"], QUESTION_ID)
                    self.assertEqual(request_bodies[5]["form_fingerprint"],
                                     resumed_preflight["metadata_fingerprint"])
                    self.assertEqual(request_bodies[5]["answer_id"], persisted["id"])
                    self.assertEqual(request_bodies[5]["expected_profile_version"],
                                     saved_profile["version"])
                    self.assertGreater(store.count("documents"), documents_before)
                    self.assertEqual(store.count("application_attempts"), attempts_before + 2)
                    self.assertEqual(store.application(applications_before[0]["id"])["state"],
                                     "DRY_RUN_COMPLETE")
                    self.assertEqual(store.application(applications_before[0]["id"])["document_version"],
                                     prepared["document_version"])
                    self.assertEqual(store.application(applications_before[1]["id"]),
                                     applications_before[1])
                    self.assertFalse(external, external)
                    self.assertFalse(errors, errors)
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated application review server must stop")


if __name__ == "__main__":
    unittest.main()
