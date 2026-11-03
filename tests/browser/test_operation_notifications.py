"""Configured notifications consume live scoped readback without writes or sends."""
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import pytest
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, now
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation


@pytest.mark.parametrize("enabled", [False, True])
def test_notification_configuration_polling_dismissal_and_profile_isolation(tmp_path, enabled):
    store = Store(f"sqlite:///{(tmp_path / 'notifications.db').as_posix()}")
    store.migrate()
    store.bind_workspace_mode("PERSONAL")
    store.save_profile(PROFILE)
    store.save_profile({**PROFILE, "id": "other", "name": "Other fixture"})
    operation = enqueue_operation(store, "DISCOVER", "fixture", {})
    foreign = enqueue_operation(store, "DISCOVER", "other", {})
    with store.engine.begin() as connection:
        connection.execute(update(m.operations).where(m.operations.c.id == foreign).values(
            state="FAILED", error="PRIVATE_OTHER_PROFILE_ERROR"))
    with patch.dict(os.environ, {"MJOBS_NOTIFICATIONS_ENABLED": "1" if enabled else "0"}):
        app = create_app(store, "http://127.0.0.1:1", tmp_path,
                         "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
    sock = bound_browser_socket()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    external, writes, errors = [], [], []
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Owned notification API failed to start"
            time.sleep(.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(10000)
                page.add_init_script("if(!localStorage.getItem('mjobs-profile')) localStorage.setItem('mjobs-profile','fixture')")

                def local_only(route):
                    if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", local_only)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: writes.append(request.url)
                        if request.method not in {"GET", "HEAD", "OPTIONS"} else None)
                page.goto(origin, wait_until="domcontentloaded")
                expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                panel = page.get_by_role("region", name="Activity notifications", exact=True)
                if not enabled:
                    expect(page.get_by_role("heading", name="Find your next opportunity.")).to_be_visible()
                    expect(panel).to_have_count(0)
                    return
                expect(panel).to_contain_text("No new activity notifications")
                with store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == operation).values(
                        state="COMPLETED", updated_at=now()))
                # No action/refresh: the bounded 15-second read-only poll observes it.
                expect(panel).to_contain_text("Job discovery completed.", timeout=22000)
                expect(panel).not_to_contain_text("needs attention")
                panel.get_by_role("button", name="Dismiss notification", exact=True).click()
                expect(panel).to_contain_text("No new activity notifications")
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                expect(panel).to_contain_text("No new activity notifications")
                expect(panel).not_to_contain_text("Job discovery completed.")
                page.get_by_label("Active profile", exact=True).select_option("other")
                expect(panel).to_contain_text("Job discovery needs attention.")
                expect(panel).not_to_contain_text("Job discovery completed.")
                expect(panel).not_to_contain_text("PRIVATE_OTHER_PROFILE_ERROR")
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_label("Active profile", exact=True)).to_have_value("other")
                expect(panel).to_contain_text("Job discovery needs attention.")
                panel.get_by_role("button", name="Mute for this visit", exact=True).click()
                expect(panel).not_to_contain_text("Job discovery needs attention.")
            finally:
                browser.close()
        assert external == [] and writes == [] and errors == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        store.engine.dispose()
        assert not thread.is_alive(), "Owned notification API did not stop"
        assert external == [] and writes == [] and errors == []
