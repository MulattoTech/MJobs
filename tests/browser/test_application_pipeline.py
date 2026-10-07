"""A manual application pipeline is visible, honest, and survives reload."""
from pathlib import Path
import asyncio
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect as async_expect
from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationPipelineBrowserTests(unittest.TestCase):
    def test_user_reported_stage_and_note_survive_reload_with_documents_linked(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-pipeline-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            from services.applications.preflight import persist_preflight
            persist_preflight(store, application_id, {
                "result": "FAIL", "reason": None, "observed_at": "2026-10-06T00:00:00Z",
                "profile_version": store.application(application_id)["profile_version"],
                "document_version": store.application(application_id)["document_version"],
                "route_id": store.application(application_id)["route_id"],
                "provider": "mock-ats", "tenant": "example", "requisition": "B",
                "metadata_fingerprint": "a" * 64, "mappings": [],
                "missing": [{"question_id": "remote", "question": "Can you work remotely?",
                             "fact_ids": [], "answer_ids": ["remote"],
                             "profile_paths": [], "document_kinds": []}],
            })
            operational_before = store.application(application_id)
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application pipeline API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()

                    board = page.get_by_role("region", name="Application pipeline")
                    expect(board).to_be_visible()
                    for label in ["Not tracked", "Saved", "Ready", "Applied — reported by you",
                                  "Recruiter Contact", "Interview", "Technical Interview",
                                  "Final Interview", "Offer", "Rejected", "Withdrawn"]:
                        expect(board.get_by_role("region", name=label, exact=True)).to_be_visible()
                    not_tracked = board.get_by_role("region", name="Not tracked")
                    card = not_tracked.get_by_role("button", name=job["title"], exact=False)
                    expect(card).to_be_visible()
                    card.click()
                    preflight = page.get_by_role("region", name="ATS question preflight")
                    expect(preflight).to_contain_text("FAIL")
                    expect(preflight.get_by_role("list", name="Unresolved required questions")).to_contain_text(
                        "Can you work remotely?")
                    expect(preflight).to_contain_text("answers: remote")
                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_contain_text("Not tracked")
                    page.get_by_label("Manual application status", exact=True).select_option("APPLIED")
                    tracking.get_by_role("button", name="Update status", exact=True).click()
                    expect(tracking).to_contain_text("Applied — reported by you")
                    expect(board.get_by_role("region", name="Applied").get_by_role(
                        "button", name=job["title"], exact=False)).to_be_visible()

                    note_text = "Synthetic note: emailed recruiter; awaiting reply."
                    page.get_by_label("Application note", exact=True).fill(note_text)
                    tracking.get_by_role("button", name="Add application note", exact=True).click()
                    history = tracking.get_by_role("list", name="Application history")
                    expect(history).to_contain_text("Applied — reported by you")
                    expect(history).to_contain_text(note_text)
                    expect(page.get_by_role("link", name="Download PDF", exact=True).first).to_be_visible()

                    after = store.application(application_id)
                    self.assertEqual(after["state"], operational_before["state"])
                    self.assertEqual(after["confirmation"], operational_before["confirmation"])
                    self.assertIsNone(after["confirmation"])
                    self.assertFalse(external)

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    applied_card = board.get_by_role("region", name="Applied").get_by_role(
                        "button", name=job["title"], exact=False)
                    expect(applied_card).to_be_visible()
                    applied_card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_contain_text("Applied — reported by you")
                    expect(tracking.get_by_role("list", name="Application history")).to_contain_text(note_text)
                    expect(page.get_by_role("link", name="Download PDF", exact=True).first).to_be_visible()
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_application_tracking_recovers_after_browser_renderer_crash(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-browser-crash-recovery-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            import services.tracking.applications as tracking_service
            original_status = tracking_service.set_application_pipeline_status
            status_committed = threading.Event()
            release_status_response = threading.Event()
            status_handler_finished = threading.Event()
            external = []

            def delay_committed_status(*args, **kwargs):
                result = original_status(*args, **kwargs)
                status_committed.set()
                if not release_status_response.wait(timeout=5):
                    raise TimeoutError("test did not release the committed status response")
                status_handler_finished.set()
                return result

            status_patcher = patch.object(
                tracking_service, "set_application_pipeline_status", delay_committed_status)
            status_patcher.start()
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()

            async def only_local(route):
                if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                    external.append(route.request.url)
                    await route.abort()
                else:
                    await route.continue_()

            async def crash_and_resume():
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated browser-crash recovery API did not start")
                    await asyncio.sleep(0.02)

                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context()
                    await context.route("**/*", only_local)
                    page = await context.new_page()
                    page.set_default_timeout(8000)
                    await page.goto(origin, wait_until="domcontentloaded")
                    await page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    await page.reload(wait_until="domcontentloaded")
                    await page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    card = board.get_by_role("region", name="Not tracked", exact=True) \
                        .get_by_role("button", name=job["title"], exact=False)
                    await card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    await page.get_by_label("Manual application status", exact=True).select_option(
                        "RECRUITER_CONTACT")

                    # Start the UI mutation, then wait until the backend has
                    # committed it but is still holding the HTTP response.
                    click_task = asyncio.create_task(
                        tracking.get_by_role("button", name="Update status", exact=True).click())
                    self.assertTrue(await asyncio.to_thread(status_committed.wait, 5),
                                    "status was not committed before the simulated crash")
                    from services.tracking.applications import read_application_tracking
                    before_crash = read_application_tracking(store, "fixture", application_id)
                    self.assertEqual(before_crash["status"], "RECRUITER_CONTACT")
                    self.assertEqual(len(before_crash["history"]), 1)

                    session = await context.new_cdp_session(page)
                    crash_event = asyncio.create_task(page.wait_for_event("crash", timeout=5000))
                    await asyncio.sleep(0)
                    crash_command = asyncio.create_task(session.send("Page.crash"))
                    try:
                        await crash_event
                    finally:
                        for task in (crash_event, crash_command, click_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(crash_event, crash_command, click_task,
                                             return_exceptions=True)

                    release_status_response.set()
                    self.assertTrue(await asyncio.to_thread(status_handler_finished.wait, 5),
                                    "committed status handler did not finish")
                    resumed = await context.new_page()
                    resumed.set_default_timeout(8000)
                    await resumed.goto(origin, wait_until="domcontentloaded")
                    await resumed.get_by_role("button", name="Applications", exact=False).click()
                    resumed_board = resumed.get_by_role("region", name="Application pipeline")
                    applied = resumed_board.get_by_role(
                        "region", name="Recruiter Contact", exact=True).get_by_role(
                            "button", name=job["title"], exact=False)
                    await async_expect(applied).to_be_visible()
                    await applied.click()
                    resumed_tracking = resumed.get_by_role("region", name="Application tracking")
                    await async_expect(resumed_tracking).to_contain_text("Recruiter Contact")
                    await async_expect(resumed_tracking.get_by_role(
                        "list", name="Application history")).to_contain_text("Recruiter Contact")
                    after_resume = read_application_tracking(store, "fixture", application_id)
                    self.assertEqual(after_resume, before_crash)
                    self.assertIsNone(store.application(application_id)["confirmation"])
                    self.assertFalse(external)
                    await context.close()
                    await browser.close()

            try:
                asyncio.run(crash_and_resume())
            finally:
                release_status_response.set()
                status_patcher.stop()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_late_status_response_cannot_leak_between_application_details(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-races-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            jobs = store.jobs("fixture")
            self.assertGreaterEqual(len(jobs), 2)
            first_job, second_job = jobs[:2]
            first_id = prepare(store, first_job["id"], "fixture", root)
            second_id = prepare(store, second_job["id"], "fixture", root)
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            import services.tracking.applications as tracking_service
            original_status = tracking_service.set_application_pipeline_status
            original_note = tracking_service.add_application_note
            status_started = threading.Event()
            release_status = threading.Event()
            note_started = threading.Event()
            release_note = threading.Event()

            def delayed_status(*args, **kwargs):
                result = original_status(*args, **kwargs)
                _, application_id, status = args[1:4]
                if application_id == first_id and status == "APPLIED":
                    status_started.set()
                    if not release_status.wait(timeout=10):
                        raise TimeoutError("test did not release delayed application status")
                return result

            def delayed_note(*args, **kwargs):
                result = original_note(*args, **kwargs)
                _, _, application_id, text = args[:4]
                if application_id == first_id and text == "Synthetic private note":
                    note_started.set()
                    if not release_note.wait(timeout=10):
                        raise TimeoutError("test did not release delayed application note")
                return result

            status_patcher = patch.object(tracking_service, "set_application_pipeline_status", delayed_status)
            note_patcher = patch.object(tracking_service, "add_application_note", delayed_note)
            status_patcher.start(); note_patcher.start()
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application pipeline API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    first_card = board.get_by_role("button", name=first_job["title"], exact=False)
                    second_card = board.get_by_role("button", name=second_job["title"], exact=False)
                    first_card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    page.get_by_label("Manual application status", exact=True).select_option("APPLIED")
                    tracking.get_by_role("button", name="Update status", exact=True).click()
                    self.assertTrue(status_started.wait(timeout=5), "status write was not held after its database commit")
                    second_card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_contain_text("Not tracked")
                    release_status.set()
                    expect(board.get_by_role("region", name="Applied — reported by you", exact=True)
                           .get_by_role("button", name=first_job["title"], exact=False)).to_be_visible()
                    expect(tracking).to_contain_text("Not tracked")

                    # Use a fresh page so no earlier details request can race this note request.
                    note_page = context.new_page()
                    note_page.set_default_timeout(8000)
                    note_page.goto(origin, wait_until="domcontentloaded")
                    note_page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    note_page.reload(wait_until="domcontentloaded")
                    note_page.get_by_role("button", name="Applications", exact=False).click()
                    note_board = note_page.get_by_role("region", name="Application pipeline")
                    note_board.get_by_role("region", name="Applied — reported by you", exact=True) \
                        .get_by_role("button", name=first_job["title"], exact=False).click()
                    note_tracking = note_page.get_by_role("region", name="Application tracking")
                    expect(note_tracking).to_contain_text("Applied — reported by you")
                    note_field = note_page.get_by_label("Application note", exact=True)
                    expect(note_field).to_be_enabled()
                    note_field.fill("Synthetic private note")
                    add_note = note_tracking.get_by_role("button", name="Add application note", exact=True)
                    expect(add_note).to_be_enabled()
                    add_note.click()
                    self.assertTrue(note_started.wait(timeout=5), "application note write was not held after its database commit")
                    note_board.get_by_role("region", name="Not tracked", exact=True) \
                        .get_by_role("button", name=second_job["title"], exact=False).click()
                    note_tracking = note_page.get_by_role("region", name="Application tracking")
                    expect(note_tracking).to_contain_text("Not tracked")
                    release_note.set()
                    expect(note_tracking).to_contain_text("Not tracked")
                    expect(note_tracking).not_to_contain_text("Synthetic private note")

                    self.assertEqual(store.application(second_id)["profile_id"], "fixture")
                    self.assertFalse(external)
                    note_page.close()
                    context.close()
                    browser.close()
            finally:
                release_status.set()
                release_note.set()
                status_patcher.stop(); note_patcher.stop()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_lost_status_and_note_responses_can_be_retried_after_edits(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-retry-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            import services.tracking.applications as tracking_service
            original_status = tracking_service.set_application_pipeline_status
            original_note = tracking_service.add_application_note
            lost_status = {"done": False}
            lost_note = {"done": False}

            def lose_status_response(*args, **kwargs):
                result = original_status(*args, **kwargs)
                _, _, target_id, status = args[:4]
                if target_id == application_id and status == "RECRUITER_CONTACT" and not lost_status["done"]:
                    lost_status["done"] = True
                    raise RuntimeError("synthetic response loss after status commit")
                return result

            def lose_note_response(*args, **kwargs):
                result = original_note(*args, **kwargs)
                _, _, target_id, text = args[:4]
                if target_id == application_id and text == "Synthetic original note" and not lost_note["done"]:
                    lost_note["done"] = True
                    raise RuntimeError("synthetic response loss after note commit")
                return result

            status_patcher = patch.object(tracking_service, "set_application_pipeline_status", lose_status_response)
            note_patcher = patch.object(tracking_service, "add_application_note", lose_note_response)
            status_patcher.start(); note_patcher.start()
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application pipeline API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    board.get_by_role("region", name="Not tracked", exact=True) \
                        .get_by_role("button", name=job["title"], exact=False).click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_contain_text("Not tracked")
                    page.get_by_label("Manual application status", exact=True).select_option("RECRUITER_CONTACT")
                    tracking.get_by_role("button", name="Update status", exact=True).click()
                    expect(page.get_by_role("alert")).to_be_visible()

                    page.get_by_label("Manual application status", exact=True).select_option("INTERVIEW")
                    expect(tracking.get_by_role("button", name="Update status", exact=True)).to_be_enabled()
                    tracking.get_by_role("button", name="Update status", exact=True).click()
                    expect(board.get_by_role("region", name="Interview", exact=True)
                           .get_by_role("button", name=job["title"], exact=False)).to_be_visible()
                    page.get_by_label("Manual application status", exact=True).select_option("RECRUITER_CONTACT")
                    expect(tracking.get_by_role("button", name="Update status", exact=True)).to_be_enabled()
                    tracking.get_by_role("button", name="Update status", exact=True).click()
                    expect(board.get_by_role("region", name="Recruiter Contact", exact=True)
                           .get_by_role("button", name=job["title"], exact=False)).to_be_visible()

                    page.get_by_label("Application note", exact=True).fill("Synthetic original note")
                    tracking.get_by_role("button", name="Add application note", exact=True).click()
                    expect(page.get_by_role("alert")).to_be_visible()
                    page.get_by_label("Application note", exact=True).fill("Synthetic revised note")
                    tracking.get_by_role("button", name="Add application note", exact=True).click()
                    history = tracking.get_by_role("list", name="Application history")
                    expect(history).to_contain_text("Synthetic original note")
                    expect(history).to_contain_text("Synthetic revised note")
                    self.assertTrue(lost_status["done"])
                    self.assertTrue(lost_note["done"])
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                status_patcher.stop(); note_patcher.stop()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
