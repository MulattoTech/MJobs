"""Connected browser flow for encrypted, profile-scoped provider credentials."""
import base64
import copy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ProviderCredentialBrowserTests(unittest.TestCase):
    def test_save_reload_rotate_test_and_delete_never_read_secret_back(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-provider-credentials-") as directory, \
                patch.dict(os.environ, {"MJOBS_CREDENTIAL_ENCRYPTION_KEY":
                                        base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")}):
            root = Path(directory)
            database = root / "credentials.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
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
                        self.fail("Credential API did not start")
                    time.sleep(0.02)

                external, errors = [], []
                with patch("services.credentials.connection.test_provider_connection",
                           return_value={"status": "CONNECTED", "code": None}), sync_playwright() as pw:
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
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="Provider credentials")
                    expect(panel).to_be_visible()
                    openai = panel.get_by_role("group", name="OpenAI")
                    secret = "synthetic-browser-credential-never-return-this"
                    field = openai.get_by_label("OpenAI credential", exact=True)
                    field.fill(secret)
                    openai.get_by_role("button", name="Save credential", exact=True).click()
                    expect(panel.get_by_text("Configured · ********", exact=False)).to_be_visible()
                    expect(field).to_have_value("")
                    self.assertNotIn(secret, panel.inner_text())

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="AI Settings", exact=True).click()
                    panel = page.get_by_role("region", name="Provider credentials")
                    expect(panel.get_by_text("Configured · ********", exact=False)).to_be_visible()
                    openai = panel.get_by_role("group", name="OpenAI")
                    check = openai.get_by_role("button", name="Test connection", exact=True)
                    check.click()
                    expect(page.get_by_role("status").filter(has_text="Provider authentication succeeded")).to_be_visible()

                    field = openai.get_by_label("OpenAI credential", exact=True)
                    field.fill("synthetic-browser-rotated-credential")
                    openai.get_by_role("button", name="Rotate credential", exact=True).click()
                    expect(panel.get_by_text("rotated 1 time", exact=False)).to_be_visible()
                    expect(field).to_have_value("")
                    page.once("dialog", lambda dialog: dialog.accept())
                    openai.get_by_role("button", name="Delete credential", exact=True).click()
                    expect(panel.get_by_text("No credential saved for this profile.")).to_be_visible()
                    browser.close()

                self.assertEqual(external, [])
                self.assertEqual(errors, [])
                with TestClient(server.config.app) as client:
                    readback = client.get("/profiles/fixture/provider-credentials")
                self.assertEqual(readback.status_code, 200, readback.text)
                self.assertNotIn("synthetic-browser-credential-never-return-this", readback.text)
                self.assertNotIn("synthetic-browser-rotated-credential", database.read_bytes().decode("latin1"))
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Credential API must stop")


if __name__ == "__main__":
    unittest.main()
