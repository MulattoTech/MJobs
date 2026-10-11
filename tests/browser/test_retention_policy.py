"""Connected browser acceptance for saved retention policy settings."""
from datetime import datetime, timedelta, timezone
import os
import json
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
from workers.transient_retention import CONFIG_ENV

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class RetentionPolicyBrowserTests(unittest.TestCase):
    def test_activity_saves_and_reloads_policy_without_running_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-retention-policy-browser-") as directory:
            root = Path(directory).resolve()
            managed = root / "transient" / "screenshots"
            managed.mkdir(parents=True)
            stale = managed / "synthetic-stale.png"
            stale.write_bytes(b"synthetic artifact; browser test never runs worker")
            old = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
            os.utime(stale, (old, old))
            summary = root / ".retention" / "last-sweep.json"
            output_dir = root / "output"
            output_dir.mkdir()
            config = {
                "configured_root": str(root),
                "allowed_roots": [str(root)],
                "rules": [{
                    "id": "browser-screenshots",
                    "category": "DEBUG_SCREENSHOT",
                    "relative_root": "transient/screenshots",
                    "pattern": "*.png",
                    "max_age_days": 30,
                }],
                "summary_path": str(summary),
                "dry_run": False,
                "prune_enabled": True,
            }
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", output_dir,
                           operation_mode="PERSONAL"),
                log_level="error",
            ))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            with patch.dict(os.environ, {CONFIG_ENV: json.dumps(config)}):
                thread.start()
                try:
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Isolated retention API did not start")
                        time.sleep(0.02)

                    external = []
                    policy_methods = []
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context()

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        page.on("request", lambda request: policy_methods.append(
                            (request.method, urlsplit(request.url).path))
                            if urlsplit(request.url).path in {
                                "/retention/policy", "/retention/preview"} else None)
                        page.goto(origin, wait_until="domcontentloaded")
                        with page.expect_response(lambda response:
                                response.request.method == "GET"
                                and urlsplit(response.url).path == "/retention/policy") as initial:
                            page.get_by_role("button", name="Activity", exact=True).click()
                        initial_response = initial.value
                        self.assertEqual(initial_response.status, 200, initial_response.text())
                        self.assertFalse(initial_response.json()["enabled"])
                        self.assertTrue(initial_response.json()["pruning_allowed"])

                        panel = page.get_by_role("region", name="Retention settings")
                        expect(panel).to_contain_text("Maximum age")
                        enabled = panel.get_by_role("checkbox", name="Enable scheduled cleanup")
                        self.assertFalse(enabled.is_checked())
                        panel.get_by_role("spinbutton", name="Maximum age for Debug screenshots").fill("12")
                        enabled.check()
                        panel.get_by_role("checkbox", name=(
                            "I understand cleanup applies only to configured transient data and never to application history."
                        )).check()

                        with page.expect_response(lambda response:
                                response.request.method == "PUT"
                                and urlsplit(response.url).path == "/retention/policy") as saved:
                            panel.get_by_role("button", name="Save retention settings").click()
                        saved_response = saved.value
                        self.assertEqual(saved_response.status, 200, saved_response.text())
                        saved_policy = saved_response.json()
                        self.assertTrue(saved_policy["enabled"])
                        self.assertTrue(saved_policy["pruning_allowed"])
                        self.assertEqual(saved_policy["revision"], 1)
                        self.assertEqual(saved_policy["rules"][0]["max_age_days"], 12)
                        self.assertTrue(stale.is_file(), "saving policy never invokes cleanup")
                        self.assertFalse(summary.exists(), "policy save must not write a sweep receipt")

                        page.reload(wait_until="domcontentloaded")
                        with page.expect_response(lambda response:
                                response.request.method == "GET"
                                and urlsplit(response.url).path == "/retention/policy") as reloaded:
                            page.get_by_role("button", name="Activity", exact=True).click()
                        reloaded_response = reloaded.value
                        self.assertEqual(reloaded_response.status, 200, reloaded_response.text())
                        self.assertEqual(reloaded_response.json()["revision"], 1)
                        self.assertTrue(reloaded_response.json()["enabled"])
                        self.assertEqual(reloaded_response.json()["rules"][0]["max_age_days"], 12)
                        panel = page.get_by_role("region", name="Retention settings")
                        expect(panel.get_by_role("checkbox", name="Enable scheduled cleanup")).to_be_checked()
                        expect(panel.get_by_role("spinbutton", name="Maximum age for Debug screenshots")).to_have_value("12")
                        self.assertTrue(stale.is_file(), "reload reads saved settings but does not clean files")
                        self.assertFalse(summary.exists())
                        self.assertGreaterEqual(policy_methods.count(("GET", "/retention/policy")), 2)
                        self.assertEqual(policy_methods.count(("PUT", "/retention/policy")), 1)
                        self.assertGreaterEqual(policy_methods.count(("GET", "/retention/preview")), 2)
                        self.assertTrue(all(
                            (method == "GET" and path in {"/retention/policy", "/retention/preview"})
                            or (method == "PUT" and path == "/retention/policy")
                            for method, path in policy_methods
                        ), "retention UI may only read policy/preview or save settings")
                        self.assertFalse(external, "browser requests must remain local")
                        context.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    sock.close()
                    store.engine.dispose()
                    self.assertFalse(thread.is_alive(), "Isolated retention API must stop")


if __name__ == "__main__":
    unittest.main()
