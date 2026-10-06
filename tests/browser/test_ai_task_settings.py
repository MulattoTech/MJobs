"""Connected browser acceptance for durable, profile-isolated AI task settings."""
import copy
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

                external, errors = [], []
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

                    context.route("**/profiles/other-profile/ai-cost-policy",
                                  unavailable_other_profile_policy)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="AI task settings")
                    expect(panel).to_be_visible()
                    self.assertEqual(panel.locator(".ai-task-settings fieldset").count(), 7)
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
                    cost_panel.get_by_label("Maximum document candidates", exact=True).fill("0")
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

                    page.locator("#active-profile").select_option("other-profile")
                    expect(panel.get_by_label("Job scoring provider", exact=True)).to_have_value("disabled")
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


if __name__ == "__main__":
    unittest.main()
