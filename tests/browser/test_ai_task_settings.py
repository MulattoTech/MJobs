"""Connected browser acceptance for durable, profile-isolated AI task settings."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from services.ai.consumer import AIJobEvaluation
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class AITaskSettingsBrowserTests(unittest.TestCase):
    @patch("apps.api.main.AITaskRouter.generate_structured",
           return_value=AIJobEvaluation(
               category="REVIEW", rationale="Check the listed infrastructure duties."))
    def test_settings_save_reload_and_remain_isolated_between_profiles(self,
                                                                      mock_generate_structured):
        with tempfile.TemporaryDirectory(prefix="mjobs-ai-task-settings-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'settings.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            first = copy.deepcopy(PROFILE)
            second = copy.deepcopy(PROFILE)
            second.update(id="other-profile", name="Other Profile", email="other@example.test")
            store.save_profile(first)
            store.save_profile(second)
            for index, description in enumerate(("Python SQL", "Terraform")):
                capture_manual_job(store, {
                    "request_id": f"browser-cost-plan-{index}",
                    "title": f"Synthetic role {index}",
                    "company": "Synthetic Company",
                    "description": description,
                    "reference_url": f"https://jobs.example.test/role-{index}",
                    "application_url": f"https://apply.example.test/role-{index}",
                    "source_details": {},
                })
            recalculate(store, "fixture")

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="PERSONAL"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated task-settings API did not start")
                    time.sleep(0.02)

                external, errors, prepared_requests = [], [], []
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1280, "height": 900})

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    def unavailable_other_profile_policy(route):
                        route.fulfill(status=503, json={"detail": "synthetic settings outage"})

                    def synthetic_application_preparation(route):
                        if route.request.method == "POST" and urlsplit(route.request.url).path == "/applications":
                            route.fulfill(status=202, json={
                                "operation_id": "synthetic-reviewed-preparation"})
                        else:
                            route.continue_()

                    context.route("**/profiles/other-profile/ai-cost-policy",
                                  unavailable_other_profile_policy)
                    context.route("**/applications", synthetic_application_preparation)
                    context.route("**/operations/synthetic-reviewed-preparation",
                                  lambda route: route.fulfill(status=200, json={
                                      "id": "synthetic-reviewed-preparation", "kind": "PREPARE",
                                      "state": "COMPLETED", "error": None, "attempt_count": 1,
                                      "cancel_requested": False, "updated_at": "synthetic",
                                  }))
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: prepared_requests.append(json.loads(request.post_data))
                            if request.method == "POST" and urlsplit(request.url).path == "/applications"
                            else None)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="AI task settings")
                    expect(panel).to_be_visible()
                    self.assertEqual(panel.locator(".ai-task-settings fieldset").count(), 7)
                    fallback_panel = panel.get_by_role("region", name="No-AI fallback modes")
                    expect(fallback_panel.locator("li")).to_have_count(7)
                    expect(fallback_panel.locator('[aria-label="Job scoring no-AI behavior"]')).to_contain_text(
                        "DETERMINISTIC")
                    expect(fallback_panel.locator('[aria-label="Resume tailoring no-AI behavior"]')).to_contain_text(
                        "do not add or rephrase claims")
                    expect(fallback_panel.locator('[aria-label="Free-form answers no-AI behavior"]')).to_contain_text(
                        "UNAVAILABLE")
                    provider = panel.get_by_label("Job scoring provider", exact=True)
                    model = panel.get_by_label("Job scoring model", exact=True)
                    expect(provider).to_have_value("disabled")
                    expect(model).to_be_disabled()
                    provider.select_option("ollama")
                    model.fill("llama3.2:3b")
                    panel.get_by_label("Resume tailoring provider", exact=True).select_option("ollama")
                    panel.get_by_label("Resume tailoring model", exact=True).fill("qwen2.5:3b")
                    panel.get_by_role("button", name="Save AI task settings", exact=True).click()
                    expect(page.get_by_role("status").filter(
                        has_text="AI task settings saved for this profile.")).to_be_visible()

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="AI task settings")
                    expect(panel.get_by_label("Job scoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Job scoring model", exact=True)).to_have_value("llama3.2:3b")
                    expect(panel.get_by_label("Resume tailoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Resume tailoring model", exact=True)).to_have_value("qwen2.5:3b")

                    cost_panel = page.get_by_role("region", name="AI cost controls")
                    expect(cost_panel).to_be_visible()
                    expect(cost_panel.get_by_label("Minimum deterministic score", exact=True)).to_have_value("75")
                    cost_panel.get_by_label("Minimum deterministic score", exact=True).fill("50")
                    cost_panel.get_by_label("Maximum jobs scanned", exact=True).fill("100")
                    cost_panel.get_by_label("Maximum AI candidates", exact=True).fill("1")
                    expect(cost_panel.get_by_label("Maximum document candidates", exact=True)).to_have_value("1")
                    cost_panel.get_by_role("button", name="Save AI cost policy", exact=True).click()
                    expect(page.get_by_role("status").filter(
                        has_text="AI cost policy saved for this profile.")).to_be_visible()
                    cost_panel.get_by_role("button", name="Preview AI work plan", exact=True).click()
                    expect(cost_panel.get_by_text(
                        "2 jobs scanned · 1 eligible · 1 AI candidate · 0 reviewed-document candidates",
                        exact=True)).to_be_visible()
                    expect(cost_panel.get_by_text(
                        "Evaluation runs only after you request it", exact=False)).to_be_visible()

                    cost_panel.get_by_role("button", name="Evaluate planned AI candidates", exact=True).click()
                    evaluations = page.get_by_role("region", name="AI evaluation results")
                    expect(evaluations).to_be_visible()
                    expect(evaluations.get_by_role("status")).to_have_text(
                        "1 candidates evaluated. These advisory results do not change deterministic matches. 0 documents created.")
                    expect(evaluations.locator("article")).to_have_count(1)
                    expect(evaluations.locator("article h4")).to_contain_text("Synthetic role")
                    expect(evaluations.locator("article h4")).to_contain_text("Synthetic Company")
                    expect(evaluations.locator("article").get_by_text("REVIEW", exact=True)).to_be_visible()
                    expect(evaluations.locator("article").get_by_text(
                        "Check the listed infrastructure duties.", exact=False)).to_be_visible()

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    cost_panel = page.get_by_role("region", name="AI cost controls")
                    expect(cost_panel.get_by_label("Minimum deterministic score", exact=True)).to_have_value("50")
                    expect(cost_panel.get_by_label("Maximum AI candidates", exact=True)).to_have_value("1")
                    evaluations = page.get_by_role("region", name="AI evaluation history")
                    expect(evaluations).to_be_visible()
                    expect(evaluations.get_by_role("article")).to_have_count(1)
                    expect(evaluations.get_by_text("Check the listed infrastructure duties.", exact=False)).to_be_visible()
                    evaluation = evaluations.get_by_role("article")
                    evaluation.get_by_label("I reviewed this AI result and approve document preparation").check()
                    evaluation.get_by_role("button", name="Confirm review and open job", exact=True).click()
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    evaluations = page.get_by_role("region", name="AI evaluation history")
                    evaluations.get_by_role("button", name="Prepare reviewed documents", exact=True).click()
                    page.get_by_role("button", name="Prepare documents", exact=True).click()
                    expect(page.get_by_role("button", name="AI Settings", exact=True)).to_be_visible()
                    self.assertTrue(prepared_requests)
                    self.assertIn("ai_evaluation_run_id", prepared_requests[-1])
                    page.get_by_role("button", name="AI Settings", exact=True).click()

                    page.locator("#active-profile").select_option("other-profile")
                    expect(panel.get_by_label("Job scoring provider", exact=True)).to_have_value("disabled")
                    expect(page.get_by_role("region", name="AI evaluation history").get_by_text(
                        "No saved AI evaluations for this profile.", exact=True)).to_be_visible()
                    expect(panel.get_by_label("Job scoring model", exact=True)).to_be_disabled()
                    expect(panel.get_by_label("Resume tailoring provider", exact=True)).to_have_value("disabled")
                    expect(panel.get_by_label("Resume tailoring model", exact=True)).to_be_disabled()
                    expect(cost_panel.get_by_label("Minimum deterministic score", exact=True)).to_have_value("75")
                    expect(cost_panel.get_by_label("Minimum deterministic score", exact=True)).to_be_disabled()
                    expect(cost_panel.get_by_label("Maximum AI candidates", exact=True)).to_have_value("75")
                    context.unroute("**/profiles/other-profile/ai-cost-policy",
                                    unavailable_other_profile_policy)
                    page.locator("#active-profile").select_option("fixture")
                    expect(panel.get_by_label("Job scoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Job scoring model", exact=True)).to_have_value("llama3.2:3b")
                    expect(panel.get_by_label("Resume tailoring provider", exact=True)).to_have_value("ollama")
                    expect(panel.get_by_label("Resume tailoring model", exact=True)).to_have_value("qwen2.5:3b")
                    expect(cost_panel.get_by_label("Minimum deterministic score", exact=True)).to_have_value("50")

                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(panel).to_be_visible()
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated task-settings API must stop")

    def test_user_can_cancel_ai_evaluation_without_persisting_partial_batch(self):
        from sqlalchemy import update
        from packages.persistence import models as m
        from services.ai.tasks import AI_TASKS

        with tempfile.TemporaryDirectory(prefix="mjobs-ai-evaluation-cancel-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'cancel.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            for index, title in enumerate(("Infrastructure Administrator", "Systems Administrator")):
                capture_manual_job(store, {
                    "request_id": f"browser-ai-cancel-{index}",
                    "title": title,
                    "company": "Synthetic Company",
                    "description": "Windows and Linux infrastructure administration.",
                    "reference_url": f"https://jobs.example.test/cancel-{index}",
                    "application_url": f"https://apply.example.test/cancel-{index}",
                    "source_details": {},
                })
            recalculate(store, "fixture")
            with store.engine.begin() as connection:
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture",
                ).values(score=95.0))
            store.save_ai_task_settings("fixture", {
                task: {"provider": "ollama", "model": "synthetic-test"}
                if task == "job_scoring" else {"provider": "disabled", "model": None}
                for task in AI_TASKS
            })

            provider_started = threading.Event()
            release_provider = threading.Event()
            provider_returned = threading.Event()
            batch_finished = threading.Event()
            provider_calls = []

            class WaitingRouter:
                def __init__(self, _configuration):
                    pass

                def generate_structured(self, **_kwargs):
                    provider_calls.append(len(provider_calls) + 1)
                    if len(provider_calls) == 1:
                        return AIJobEvaluation(category="STRONG", rationale="Synthetic completed result.")
                    provider_started.set()
                    if not release_provider.wait(60):
                        raise AssertionError("browser test did not release the bounded provider call")
                    provider_returned.set()
                    return AIJobEvaluation(category="REVIEW", rationale="Synthetic interrupted result.")

            from apps.api.main import consume_ai_candidates as original_consume

            def consume_and_signal(*args, **kwargs):
                try:
                    return original_consume(*args, **kwargs)
                finally:
                    batch_finished.set()

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="PERSONAL"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("AI-evaluation cancellation API did not start")
                    time.sleep(0.02)

                external, errors = [], []
                with patch("apps.api.main.AITaskRouter", WaitingRouter), \
                        patch("apps.api.main.consume_ai_candidates", consume_and_signal), \
                        sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1280, "height": 900})

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(2000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="AI cost controls")
                    panel.get_by_role("button", name="Preview AI work plan", exact=True).click()
                    panel.get_by_role("button", name="Evaluate planned AI candidates", exact=True).click()
                    self.assertTrue(provider_started.wait(5), "second synthetic provider call did not start")
                    page.get_by_role("button", name="Cancel AI evaluation", exact=True).click()
                    expect(page.get_by_role("status").filter(
                        has_text="Cancellation accepted. Waiting for the bounded provider call; partial results will not be saved.")).to_be_visible()
                    self.assertFalse(batch_finished.is_set(), "cancel acknowledgement must wait for the provider call")
                    release_provider.set()
                    self.assertTrue(provider_returned.wait(5), "bounded provider call was not released")
                    self.assertTrue(batch_finished.wait(5), "AI batch did not finish cancellation handling")
                    expect(page.get_by_role("status").filter(
                        has_text="AI evaluation canceled. The interrupted batch was discarded.")).to_be_visible()
                    self.assertEqual(provider_calls, [1, 2])

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    evaluations = page.get_by_role("region", name="AI evaluation history")
                    expect(evaluations.get_by_text(
                        "No saved AI evaluations for this profile.", exact=True)).to_be_visible()
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    browser.close()
            finally:
                release_provider.set()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "AI-evaluation cancellation API must stop")


if __name__ == "__main__":
    unittest.main()
