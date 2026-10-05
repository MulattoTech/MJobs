"""One synthetic browser journey through the core MJobs workflow."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.browser.test_profile_import import resume_pdf


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class CoreWorkflowTrialBrowserTests(unittest.TestCase):
    def test_import_capture_prepare_answers_track_and_reopen(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-core-workflow-trial-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'trial.db').as_posix()}"
            errors, external = [], []

            def start_api(store):
                app = create_app(store, "http://127.0.0.1:1", root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                thread = threading.Thread(target=server.run,
                                          kwargs={"sockets": [sock]}, daemon=True)
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated core-trial API did not start")
                    time.sleep(0.02)
                return origin, sock, server, thread

            def stop_api(store, sock, server, thread):
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            origin, sock, server, thread = start_api(store)
            first_resume_bytes = None
            old_resume_digest = None
            cover_digest = None
            answer_digest = None
            profile_id = application_id = job_id = None
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)

                    def new_context():
                        context = browser.new_context(accept_downloads=True)

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)

                        def execute_rescore(route):
                            response = route.fetch()
                            operation_id = response.json()["operation_id"]
                            with patch.dict("os.environ", MJOBS_DATABASE_URL=database_url,
                                            MJOBS_OPERATION_MODE="FIXTURE",
                                            MJOBS_OUTPUT_DIR=str(root)):
                                from workers.tasks import execute_operation
                                outcome = execute_operation(operation_id)
                            self.assertEqual(outcome["status"], "COMPLETED", outcome)
                            route.fulfill(response=response)

                        context.route("**/matches", execute_rescore)
                        return context

                    context = new_context()
                    page = context.new_page()
                    page.set_default_timeout(12000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    main_nav = page.get_by_role("navigation", name="Main navigation")

                    def navigate(label):
                        main_nav.get_by_role("button").filter(has_text=label).click()

                    expect(page.get_by_text("External submission OFF", exact=True)).to_be_visible()

                    # Create a synthetic profile, import its resume, and add the
                    # extracted suggestions for deliberate user review.
                    navigate("Profile")
                    page.get_by_label("Full name", exact=True).fill("Synthetic Core Applicant")
                    page.get_by_label("Email", exact=True).fill("core-applicant@example.invalid")
                    page.get_by_role("button", name="Save profile", exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
                    expect(page.locator("#active-profile")).not_to_have_value("")
                    profile_id = page.locator("#active-profile").input_value()
                    page.get_by_label("Choose a resume (up to 10 MiB)").set_input_files(
                        {"name": "synthetic-resume.pdf", "mimeType": "application/pdf",
                         "buffer": resume_pdf()})
                    page.get_by_label("Resume label", exact=True).fill("Synthetic IT Resume")
                    page.get_by_role("button", name="Store resume", exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Resume stored locally")).to_be_visible()
                    page.get_by_role("button", name="Extract and review", exact=True).click()
                    review = page.get_by_role("region", name="Resume extraction review")
                    expect(review).to_be_visible()
                    expect(review.get_by_text("Maintained support@example.invalid routing.",
                                              exact=False)).to_be_visible()
                    review.get_by_role("button", name="Add selected to profile editor",
                                       exact=True).click()

                    # Keep imported facts pending, and deliberately approve only
                    # the two synthetic skills used by this job.
                    for value in ("Python", "SQL"):
                        page.get_by_role("button", name="Add fact", exact=True).click()
                        fact = page.locator("fieldset.fact").last
                        fact.get_by_role("combobox").select_option("skill")
                        fact.get_by_label("Fact text", exact=True).fill(value)
                        fact.get_by_label("Evidence / source", exact=True).fill(
                            "Reviewed from synthetic resume")
                        fact.get_by_label("I verified this fact", exact=True).check()
                        fact.get_by_label("I approve its use in documents", exact=True).check()
                    page.get_by_role("button", name="Add fact", exact=True).click()
                    pending = page.locator("fieldset.fact").last
                    pending.get_by_role("combobox").select_option("skill")
                    pending.get_by_label("Fact text", exact=True).fill("Kubernetes")
                    pending.get_by_label("Evidence / source", exact=True).fill(
                        "Unreviewed synthetic suggestion")

                    # Save a reviewed exact-question answer. It remains a
                    # suggestion until selected and reviewed for this application.
                    page.get_by_role("button", name="Add answer", exact=True).click()
                    saved_answer = page.locator("fieldset.fact").last
                    saved_answer.get_by_label("Question this answer addresses").fill(
                        "How many years of experience?")
                    saved_answer.get_by_label("Answer text", exact=True).fill(
                        "Five years of user-confirmed experience.")
                    saved_answer.get_by_label("Provenance", exact=True).fill(
                        "Reviewed synthetic profile")
                    saved_answer.get_by_label("I reviewed and verified this answer").check()
                    saved_answer.get_by_label("I approve exact reuse for this question").check()
                    page.get_by_role("button", name="Save profile and answers", exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Reusable answers saved")).to_be_visible()
                    profile = store.profile(profile_id)
                    reviewed = [row for row in profile["facts"]
                                if row["source"] == "Reviewed from synthetic resume"]
                    self.assertEqual({row["text"] for row in reviewed}, {"Python", "SQL"})
                    self.assertTrue(all(row["verified"] and row["user_approved"]
                                        for row in reviewed), reviewed)
                    self.assertFalse(next(row for row in profile["facts"]
                                          if row["text"] == "Kubernetes")["user_approved"])
                    self.assertTrue(profile["answers"][0]["user_approved"])

                    # Persist an explicit search filter in the UI without
                    # triggering external source collection.
                    navigate("Sources")
                    page.get_by_label("Board token", exact=True).fill("synthetic-board")
                    page.get_by_role("button", name="Add board", exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Source saved")).to_be_visible()
                    source_rows = context.request.get(origin + "/sources").json()["sources"]
                    self.assertEqual(len(source_rows), 1)
                    source = source_rows[0]
                    # The source tab does not refresh its local list after a
                    # board is created, so reload through the normal UI readback.
                    page.reload(wait_until="domcontentloaded")
                    navigate("Sources")
                    search_form = page.locator("form").filter(
                        has=page.get_by_label("Search name", exact=True))
                    search_form.get_by_label("Search name", exact=True).fill(
                        "Synthetic Systems Roles")
                    search_form.get_by_role("combobox").nth(0).select_option(source["id"])
                    search_form.get_by_role("combobox").nth(1).select_option("ANY")
                    search_form.get_by_role("combobox").nth(2).select_option("FULL_TIME")
                    search_form.get_by_label("Include titles", exact=True).fill(
                        "Systems Administrator")
                    search_form.get_by_role("button", name="Save scheduled search",
                                            exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Search profile saved")).to_be_visible()
                    saved_search = context.request.get(
                        origin + "/search-profiles?profile_id=" + profile_id).json()["search_profiles"]
                    self.assertEqual(saved_search[0]["criteria"]["include_titles"],
                                     ["Systems Administrator"])
                    self.assertEqual(saved_search[0]["criteria"]["employment_types"], ["FULL_TIME"])

                    # Capture the job manually; its reference URL is inert and
                    # never fetched. The unapproved Kubernetes requirement must
                    # remain UNKNOWN in the explainable result.
                    navigate("Jobs")
                    page.get_by_role("button", name="Capture a job", exact=True).first.click()
                    capture_form = page.get_by_role("form", name="Manual job capture")
                    capture_form.get_by_label("Job title", exact=True).fill("Systems Administrator")
                    capture_form.get_by_label("Company", exact=True).fill("Synthetic Infrastructure")
                    description = ("Maintain Windows infrastructure with Python and SQL. "
                                   "Kubernetes is listed as preferred experience.")
                    capture_form.get_by_label("Job description", exact=True).fill(description)
                    capture_form.get_by_label("Location", exact=True).fill("Tulsa, OK")
                    capture_form.get_by_role("combobox").nth(0).select_option("ONSITE")
                    capture_form.get_by_label("Job reference URL", exact=True).fill(
                        "http://127.0.0.1:9/private/reference")
                    capture_form.get_by_label("Application URL", exact=True).fill(
                        "https://apply.example.invalid/synthetic-systems")
                    with page.expect_response(
                        lambda response: response.url == origin + "/jobs/manual"
                        and response.request.method == "POST"
                    ) as captured:
                        capture_form.get_by_role("button", name="Save captured job", exact=True).click()
                    response = captured.value
                    self.assertEqual(response.status, 201)
                    job_id = response.json()["job"]["id"]
                    expect(page.get_by_role("status").filter(
                        has_text="Job captured and evaluated")).to_be_visible()
                    page.get_by_label("Search jobs", exact=True).fill("Systems Administrator")
                    page.get_by_label("Work location", exact=True).select_option("ONSITE")
                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    explanation = detail.get_by_role("list", name="Skill match explanation")
                    expect(explanation).to_contain_text("Python")
                    expect(explanation).to_contain_text("Supported by an approved career fact")
                    expect(explanation).to_contain_text("Kubernetes")
                    expect(explanation).to_contain_text("UNKNOWN")
                    expect(explanation).to_contain_text("No approved career fact supports this skill")
                    self.assertTrue(store.job(job_id, profile_id=profile_id)["match"])
                    self.assertFalse(any("/private/reference" in request
                                         for request in external))

                    page.get_by_label("Cover letter length", exact=True).select_option("DETAILED")
                    page.get_by_label("Cover letter style", exact=True).select_option("warm")
                    with page.expect_response(
                        lambda item: item.url == origin + "/applications"
                        and item.request.method == "POST"
                    ) as queued:
                        page.get_by_role("button", name="Prepare documents", exact=True).click()
                    operation = queued.value.json()
                    with patch.dict("os.environ", MJOBS_DATABASE_URL=database_url,
                                    MJOBS_MOCK_ORIGIN="http://127.0.0.1:1",
                                    MJOBS_OUTPUT_DIR=str(root), MJOBS_OPERATION_MODE="FIXTURE"):
                        from workers.tasks import execute_operation
                        outcome = execute_operation(operation["operation_id"])
                    self.assertEqual(outcome["status"], "COMPLETED", outcome)
                    expect(page.get_by_text("Preview validated cover letter", exact=True)).to_be_visible()
                    application_id = store.applications(profile_id)[0]["id"]
                    app_documents = context.request.get(
                        origin + f"/profiles/{profile_id}/applications/{application_id}/documents").json()["documents"]
                    old_resume = next(row for row in app_documents if row["kind"] == "resume")
                    cover = next(row for row in app_documents if row["kind"] == "cover-letter")
                    resume_download = context.request.get(
                        origin + f"/profiles/{profile_id}/documents/{old_resume['id']}/txt")
                    cover_download = context.request.get(
                        origin + f"/profiles/{profile_id}/documents/{cover['id']}/txt")
                    self.assertTrue(resume_download.ok and cover_download.ok)
                    first_resume_bytes = resume_download.body()
                    first_cover_bytes = cover_download.body()
                    old_resume_digest = hashlib.sha256(first_resume_bytes).hexdigest()
                    cover_digest = hashlib.sha256(first_cover_bytes).hexdigest()
                    self.assertIn(b"Python", first_resume_bytes)
                    self.assertIn(b"Python", first_cover_bytes)
                    self.assertNotIn(b"Kubernetes", first_resume_bytes)
                    self.assertEqual(old_resume["sha256"]["txt"], old_resume_digest)
                    self.assertEqual(cover["sha256"]["txt"], cover_digest)

                    # Explicitly select the saved answer, review it for this
                    # job, and preserve its immutable export beside the docs.
                    navigate("Applications")
                    page.locator(".job-card").filter(has_text="Systems Administrator").click()
                    page.get_by_role("button", name="Add question", exact=True).click()
                    page.get_by_label("Employer question").fill("HOW MANY YEARS OF EXPERIENCE?")
                    page.get_by_role("button", name="Use saved answer (review required)",
                                     exact=True).evaluate("button => button.click()")
                    expect(page.get_by_role("textbox", name="Answer", exact=True)).to_have_value(
                        "Five years of user-confirmed experience.")
                    export = page.get_by_role("button", name="Save reviewed answers and export TXT",
                                              exact=True)
                    expect(export).to_be_disabled()
                    page.get_by_label("I reviewed this exact answer for this job").check()
                    expect(export).to_be_enabled()
                    export.click()
                    exported = page.get_by_role("region", name="application-answer-export version 1",
                                                exact=True)
                    with page.expect_download() as downloaded_answer:
                        exported.get_by_role("link", name="Download TXT", exact=True).click()
                    answer_bytes = Path(downloaded_answer.value.path()).read_bytes()
                    self.assertIn(b"Five years of user-confirmed experience.", answer_bytes)
                    answer_digest = hashlib.sha256(answer_bytes).hexdigest()

                    # Manual status is an audit event, not a submission receipt.
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    not_tracked = pipeline.get_by_role("region", name="Not tracked", exact=True)
                    card = not_tracked.get_by_role("button", name="Systems Administrator", exact=False)
                    expect(card).to_be_visible()
                    card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    status_select = tracking.get_by_label("Manual application status", exact=True)
                    status_select.select_option("APPLIED")
                    expect(status_select).to_have_value("APPLIED")
                    expect(tracking.get_by_role("button", name="Update status", exact=True)).to_be_enabled()
                    tracking.get_by_role("button", name="Update status", exact=True).evaluate(
                        "button => button.click()")
                    expect(tracking.get_by_role("list", name="Application history")).to_contain_text(
                        "Applied — reported by you")
                    note = "Synthetic note: application sent manually; await recruiter reply."
                    page.get_by_label("Application note", exact=True).fill(note)
                    tracking.get_by_role("button", name="Add application note", exact=True).click()
                    expect(tracking.get_by_role("list", name="Application history")).to_contain_text(note)
                    operational = store.application(application_id)
                    self.assertIsNone(operational["confirmation"])
                    self.assertNotEqual(operational["state"], "APPLIED")
                    self.assertFalse(external)
                    self.assertEqual(context.request.get(origin + "/health").json()[
                        "external_submission_enabled"], False)

                    context.close()
                    browser.close()
            finally:
                stop_api(store, sock, server, thread)

            # Restart persistence layer and API against the same temporary
            # database/files, then verify the tracker and prior downloads.
            store = Store(database_url)
            store.bind_workspace_mode("FIXTURE")
            origin, sock, server, thread = start_api(store)
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(12000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    main_nav = page.get_by_role("navigation", name="Main navigation")

                    def navigate(label):
                        main_nav.get_by_role("button").filter(has_text=label).click()

                    page.evaluate(f"localStorage.setItem('mjobs-profile', {profile_id!r})")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile")).to_have_value(profile_id)
                    self.assertTrue(context.request.get(
                        origin + f"/profiles/{profile_id}").json()["facts"])
                    persisted_search = context.request.get(
                        origin + f"/search-profiles?profile_id={profile_id}").json()["search_profiles"]
                    self.assertEqual(persisted_search[0]["criteria"]["include_titles"],
                                     ["Systems Administrator"])
                    navigate("Applications")
                    applied = page.get_by_role("region", name="Applied — reported by you", exact=True)
                    card = applied.get_by_role("button", name="Systems Administrator", exact=False)
                    expect(card).to_be_visible()
                    card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_contain_text("Applied — reported by you")
                    expect(tracking.get_by_role("list", name="Application history")).to_contain_text(
                        "Synthetic note: application sent manually; await recruiter reply.")
                    answer_region = page.get_by_role("region", name="application-answer-export version 1",
                                                     exact=True)
                    with page.expect_download() as answer_download:
                        answer_region.get_by_role("link", name="Download TXT", exact=True).click()
                    self.assertEqual(hashlib.sha256(Path(answer_download.value.path()).read_bytes()).hexdigest(),
                                     answer_digest)
                    resume_region = page.get_by_role("region", name="resume version 1", exact=True)
                    with page.expect_download() as resume_download:
                        resume_region.get_by_role("link", name="Download TXT", exact=True).click()
                    self.assertEqual(hashlib.sha256(Path(resume_download.value.path()).read_bytes()).hexdigest(),
                                     old_resume_digest)
                    doc_region = page.get_by_role("region", name="cover-letter version 1", exact=True)
                    with page.expect_download() as cover_download:
                        doc_region.get_by_role("link", name="Download TXT", exact=True).click()
                    self.assertEqual(hashlib.sha256(Path(cover_download.value.path()).read_bytes()).hexdigest(),
                                     cover_digest)
                    self.assertEqual(hashlib.sha256(first_cover_bytes).hexdigest(), cover_digest)
                    profile = store.profile(profile_id)
                    self.assertFalse(next(row for row in profile["facts"]
                                          if row["text"] == "Kubernetes")["user_approved"])
                    self.assertIsNone(store.application(application_id)["confirmation"])
                    self.assertFalse(external)
                    self.assertEqual(errors, [], errors)
                    context.close()
                    browser.close()
            finally:
                stop_api(store, sock, server, thread)


if __name__ == "__main__":
    unittest.main()
