"""Connected browser proof that Activity previews retention without pruning."""
from datetime import datetime, timedelta, timezone
import json
import os
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
class TransientRetentionBrowserTests(unittest.TestCase):
    def test_activity_preview_survives_get_and_reload_without_deleting_files(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-retention-browser-") as directory:
            root = Path(directory)
            managed = root / "transient" / "screenshots"
            managed.mkdir(parents=True)
            summary = root / ".retention" / "last-sweep.json"
            stale = managed / "synthetic-stale.png"
            stale.write_bytes(b"synthetic stale screenshot")
            old_timestamp = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
            os.utime(stale, (old_timestamp, old_timestamp))
            fresh = managed / "synthetic-fresh.png"
            fresh.write_bytes(b"synthetic fresh screenshot")
            unmatched = managed / "synthetic-stale.html"
            unmatched.write_bytes(b"unmatched old file")
            os.utime(unmatched, (old_timestamp, old_timestamp))
            core_history = root / "applications" / "history.json"
            core_history.parent.mkdir()
            core_history.write_bytes(b"synthetic application history")

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
                "dry_run": True,
                "prune_enabled": False,
            }
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            output_dir = root / "output"
            output_dir.mkdir()
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
                    retention_methods = []
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
                        page.on("request", lambda request: retention_methods.append(
                            (request.method, urlsplit(request.url).path))
                            if urlsplit(request.url).path == "/retention/preview" else None)
                        page.goto(origin, wait_until="domcontentloaded")
                        with page.expect_response(lambda response:
                                response.request.method == "GET"
                                and urlsplit(response.url).path == "/retention/preview") as first:
                            page.get_by_role("button", name="Activity", exact=True).click()
                        first_response = first.value
                        self.assertEqual(first_response.status, 200, first_response.text())
                        first_payload = first_response.json()
                        self.assertTrue(first_payload["preview"]["dry_run"])
                        self.assertEqual(first_payload["preview"]["candidate_count"], 1)
                        self.assertEqual(first_payload["preview"]["removed_count"], 0)

                        panel = page.get_by_role("region", name="Transient data retention")
                        expect(panel).to_contain_text("Debug screenshots")
                        expect(panel).to_contain_text("1 item ·")
                        expect(panel).to_contain_text("Last sweep: Not run yet")
                        expect(panel).not_to_contain_text("Delete listed temporary data")
                        self.assertTrue(stale.is_file(), "GET preview must not prune the stale artifact")
                        self.assertTrue(fresh.is_file(), "fresh artifacts are not cleanup candidates")
                        self.assertTrue(unmatched.is_file(), "unmatched artifacts are preserved")
                        self.assertTrue(core_history.is_file(), "core application history is outside managed scope")
                        self.assertFalse(summary.exists(), "read-only preview must not persist a sweep receipt")

                        page.reload(wait_until="domcontentloaded")
                        with page.expect_response(lambda response:
                                response.request.method == "GET"
                                and urlsplit(response.url).path == "/retention/preview") as second:
                            page.get_by_role("button", name="Activity", exact=True).click()
                        second_response = second.value
                        self.assertEqual(second_response.status, 200, second_response.text())
                        expect(panel).to_contain_text("1 item ·")
                        expect(panel).to_contain_text("Last sweep: Not run yet")
                        self.assertEqual(second_response.json()["preview"]["removed_count"], 0)
                        self.assertTrue(stale.is_file(), "reload and preview must remain read-only")
                        self.assertTrue(unmatched.is_file())
                        self.assertTrue(core_history.is_file())
                        self.assertFalse(summary.exists())
                        self.assertFalse(external, "browser traffic must remain local")
                        self.assertEqual(retention_methods, [("GET", "/retention/preview"),
                                                             ("GET", "/retention/preview")])
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
