"""Connected browser proof for profile-scoped company discovery controls."""
from pathlib import Path
from fixtures.browser_ports import bound_browser_socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class CompanyDiscoveryPolicyBrowserTests(unittest.TestCase):
    def test_policy_saves_and_survives_reload_without_external_requests(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-company-policy-browser-") as directory:
            database = (Path(directory) / "browser.db").as_posix()
            store = Store(f"sqlite:///{database}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(PROFILE.copy())
            app = create_app(store, "http://127.0.0.1:1", ROOT,
                             "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated company-policy API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
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
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option(str(PROFILE["id"]))
                    page.get_by_role("button", name="Profile", exact=True).click()
                    enabled = page.get_by_label("Enable company discovery", exact=False)
                    expect(enabled).to_be_checked()
                    verification = page.get_by_label("Verify employer links from imported jobs", exact=True)
                    expect(verification).not_to_be_checked()
                    expect(verification).to_be_enabled()

                    def save_profile():
                        button = page.get_by_role("button", name="Save profile", exact=True).first
                        with page.expect_response(lambda response: response.request.method == "PUT"
                                                   and urlsplit(response.url).path == "/profiles") as saved:
                            button.click()
                        self.assertEqual(saved.value.status, 200)
                        expect(button).to_be_enabled()

                    verification.check()
                    self.assertEqual(store.profile(PROFILE["id"])["preferences"]["company_discovery"]
                                     ["employer_verification"]["mode"], "DISABLED",
                                     "An unsaved checkbox must not grant authorization")
                    save_profile()
                    first = store.profile(PROFILE["id"])["preferences"]["company_discovery"]["employer_verification"]
                    self.assertEqual(first["mode"], "GREENHOUSE_RECIPROCAL_V1")
                    self.assertRegex(first["activation_id"], r"^[0-9a-f]{32}$")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Profile", exact=True).click()
                    expect(verification).to_be_checked()
                    verification.uncheck()
                    save_profile()
                    revoked = store.profile(PROFILE["id"])["preferences"]["company_discovery"]["employer_verification"]
                    self.assertEqual(revoked, {"version": 1, "mode": "DISABLED", "activation_id": None})
                    verification.check()
                    save_profile()
                    second = store.profile(PROFILE["id"])["preferences"]["company_discovery"]["employer_verification"]
                    self.assertEqual(second["mode"], "GREENHOUSE_RECIPROCAL_V1")
                    self.assertNotEqual(second["activation_id"], first["activation_id"])
                    expect(page.get_by_label("Minimum match score for discovery")).to_have_value("60")
                    expect(page.get_by_label("Match score for priority discovery")).to_have_value("80")
                    expect(page.get_by_label("Maximum new company discoveries per hour")).to_have_value("12")
                    enabled.uncheck()
                    expect(verification).not_to_be_checked()
                    expect(verification).to_be_disabled()
                    page.get_by_label("Minimum match score for discovery").fill("65")
                    page.get_by_label("Match score for priority discovery").fill("90")
                    page.get_by_label("Maximum new company discoveries per hour").fill("4")
                    with page.expect_response(lambda response: response.request.method == "PUT"
                                               and urlsplit(response.url).path == "/profiles"):
                        page.get_by_role("button", name="Save profile", exact=True).first.click()
                    expect(enabled).not_to_be_checked()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Profile", exact=True).click()
                    expect(page.get_by_label("Enable company discovery", exact=False)).not_to_be_checked()
                    expect(page.get_by_label("Minimum match score for discovery")).to_have_value("65")
                    expect(page.get_by_label("Match score for priority discovery")).to_have_value("90")
                    expect(page.get_by_label("Maximum new company discoveries per hour")).to_have_value("4")
                    expect(page.get_by_text("This never submits applications.", exact=False)).to_be_visible()
                    expect(verification).not_to_be_checked()
                    expect(verification).to_be_disabled()
                    self.assertEqual(store.profile(PROFILE["id"])["preferences"]["company_discovery"]
                                     ["employer_verification"],
                                     {"version": 1, "mode": "DISABLED", "activation_id": None})
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated company-policy API must stop")


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class EmployerVerificationOptInHarnessBrowserTests(unittest.TestCase):
    def test_actual_normal_save_baseline_and_later_admission_drift_without_egress(self):
        """Exercise the harness helper against the built UI and real local persistence."""
        import copy
        from datetime import datetime, timezone
        import hashlib
        import json
        from unittest.mock import patch

        from cli import ui_e2e
        from packages.domain.employer_verification import verification_policy_snapshot

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":"), allow_nan=False).encode()).hexdigest()

        with tempfile.TemporaryDirectory(prefix="mjobs-employer-opt-in-browser-") as directory:
            database = (Path(directory) / "browser.db").as_posix()
            store = Store(f"sqlite:///{database}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile_input = copy.deepcopy(PROFILE)
            profile_input.update(id="employer-opt-in-browser", facts=[], answers=[])
            profile_id = profile_input["id"]
            store.save_profile(profile_input)
            initial = store.profile(profile_id)
            self.assertEqual(initial["preferences"]["company_discovery"]["employer_verification"],
                             {"version": 1, "mode": "DISABLED", "activation_id": None})
            app = create_app(store, "http://127.0.0.1:1", ROOT,
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
                        self.fail("Isolated opt-in harness API did not start")
                    time.sleep(0.02)
                with patch.object(store, "save_profile", wraps=store.save_profile) as actual_saves, \
                        sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external, prohibited, browser_writes = [], [], []

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if (parsed.scheme, parsed.netloc) != ("http", urlsplit(origin).netloc):
                            external.append(route.request.url)
                            route.abort()
                        elif route.request.method in {"GET", "HEAD"}:
                            route.continue_()
                        elif route.request.method == "PUT" and parsed.path == "/profiles":
                            browser_writes.append(route.request.post_data_json)
                            route.continue_()
                        else:
                            prohibited.append((route.request.method, parsed.path))
                            route.abort()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option(profile_id)
                    page.get_by_role("button", name="Profile", exact=True).click()
                    control = page.get_by_label("Verify employer links from imported jobs", exact=True)
                    expect(control).not_to_be_checked()

                    def get_json(path):
                        self.assertIn(urlsplit(path).path, {
                            "/settings", "/profiles/" + profile_id,
                            "/profiles/" + profile_id + "/job-inventory"})
                        response = context.request.get(origin + path)
                        self.assertEqual(response.status, 200)
                        return response.json()

                    def normal_save():
                        page.get_by_role("button", name="Save profile", exact=True).click()
                        expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
                        ui_e2e.wait_for_ui_action_idle(page, "Save profile")

                    settings = get_json("/settings")
                    self.assertEqual(settings["operation_mode"], "FIXTURE")
                    self.assertIs(settings["external_submission_enabled"], False)
                    witness = ui_e2e.observe_employer_verification_opt_in(
                        page, get_json, normal_save, profile_id)
                    saved = get_json("/profiles/" + profile_id)
                    activation = saved["preferences"]["company_discovery"]["employer_verification"]["activation_id"]
                    self.assertRegex(activation, r"^[0-9a-f]{32}$")
                    self.assertEqual(actual_saves.call_count, 1)
                    incoming = actual_saves.call_args.args[0]
                    self.assertIsNone(incoming["preferences"]["company_discovery"]["employer_verification"]["activation_id"])
                    self.assertEqual(actual_saves.call_args.kwargs["expected_version"], initial["version"])
                    self.assertIs(actual_saves.call_args.kwargs["require_expected_version"], True)
                    self.assertEqual(browser_writes[0]["expected_version"], initial["version"])
                    self.assertIsNone(browser_writes[0]["preferences"]["company_discovery"]["employer_verification"]["activation_id"])
                    self.assertNotEqual(saved["version"], initial["version"])
                    self.assertEqual(witness["before_profile_version"], initial["version"])
                    self.assertEqual(witness["saved_profile_version"], saved["version"])
                    self.assertEqual(witness["result"], "SAVED_PENDING_BASELINE")
                    self.assertEqual(witness["scope"], "PROFILE_UI_SAVE_ONLY")
                    self.assertEqual(witness["activation_id_sha256"], digest(activation))
                    self.assertEqual(witness["saved_policy_sha256"], digest(verification_policy_snapshot(saved)))
                    self.assertNotIn(activation, json.dumps(witness))
                    self.assertEqual(len(witness["checks"]), 7)
                    self.assertTrue(all(value is True for value in witness["checks"].values()))

                    page.get_by_label("Full name", exact=True).fill("Updated Synthetic Opt-in Applicant")
                    normal_save()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Profile", exact=True).click()
                    expect(control).to_be_checked()
                    current = get_json("/profiles/" + profile_id)
                    self.assertNotEqual(current["version"], saved["version"])
                    self.assertEqual(current["preferences"]["company_discovery"]["employer_verification"]["activation_id"], activation)
                    inventory = ui_e2e.collect_onboarding_job_inventory(get_json, profile_id)
                    self.assertEqual(inventory["schema_version"], 4)
                    self.assertEqual(inventory["scope"], "ALL_PERSISTED_JOBS")
                    self.assertEqual(inventory["profile_version"], current["version"])
                    self.assertEqual(inventory["job_count"], 0)
                    bound = ui_e2e.bind_employer_verification_baseline(witness, current, inventory)
                    self.assertEqual(bound["saved_profile_version"], saved["version"])
                    self.assertEqual(bound["profile_version"], current["version"])
                    self.assertEqual(bound["activation_id_sha256"], witness["activation_id_sha256"])
                    self.assertEqual(bound["policy_sha256"], digest(verification_policy_snapshot(current)))
                    # Controlled profile-UI metadata only, never a campaign or public observation.
                    source = {"commit": "a" * 40, "tree": "b" * 40}
                    artifact = {"profile_id": profile_id, "mode": "ONBOARDING_FIXTURE",
                        "source": source, "acceptance_run_id": "employer-opt-in-browser",
                        "target_origin": origin, "external_submission_enabled": False,
                        "timestamp": datetime.now(timezone.utc).isoformat(), "job_inventory": inventory,
                        "employer_verification_opt_in": {**bound, "source": source,
                            "acceptance_run_id": "employer-opt-in-browser", "target_origin": origin}}
                    ui_e2e.validate_employer_verification_witness(artifact, True, current_profile=current)
                    self.assertNotIn("acceptance_observations", artifact)
                    self.assertNotIn("activation_id", artifact["employer_verification_opt_in"])

                    # A later real normal save must invalidate this earlier baseline at admission.
                    page.get_by_label("Full name", exact=True).fill("Later Synthetic Opt-in Applicant")
                    normal_save()
                    changed = get_json("/profiles/" + profile_id)
                    self.assertNotEqual(changed["version"], current["version"])
                    with self.assertRaises(ValueError):
                        ui_e2e.validate_employer_verification_witness(artifact, True, current_profile=changed)
                    self.assertEqual(actual_saves.call_count, 3)
                    self.assertEqual(len(browser_writes), 3)
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    self.assertEqual(prohibited, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated opt-in harness API must stop")
