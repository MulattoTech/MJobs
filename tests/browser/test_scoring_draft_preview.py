"""Connected no-write preview, draft invalidation and stale in-flight response guards."""
import json
import threading
import time
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
import uvicorn

from fixtures.browser_ports import bound_browser_socket
from tests.integration.test_preference_weighting import app_for, configure, create_workspace, rescore, snapshot
from tests.integration.test_scoring_draft_preview import PREVIEW_URL


def test_real_preview_is_read_only_and_edits_hide_previous_and_late_results(tmp_path):
    store = create_workspace(tmp_path)
    app = app_for(store, tmp_path)
    with TestClient(app) as client:
        saved = configure(client)
    rescore(store)
    before = snapshot(store)
    sock = bound_browser_socket()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    outside, forbidden, previews, held = [], [], [], []
    hold_next = False
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()

            def boundary(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != urlsplit(origin).netloc:
                    outside.append(request.url)
                    route.abort()
                elif request.method == "POST" and parsed.path == PREVIEW_URL:
                    previews.append(request.post_data_json)
                    if hold_next:
                        held.append(route)
                    else:
                        route.continue_()
                elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                    forbidden.append((request.method, parsed.path))
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", boundary)
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(origin, wait_until="domcontentloaded")
            page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
            page.reload(wait_until="domcontentloaded")
            page.get_by_label("Saved search profile", exact=True).last.select_option("preference-search")
            editor = page.locator("section").filter(has=page.get_by_role(
                "heading", name="Role-specific scoring profile", exact=True))
            expect(editor.get_by_label("Title weight", exact=True)).to_have_value("10")
            for label in ("Skills", "Salary", "Work arrangement", "Employment type", "Remote work region"):
                editor.get_by_label(f"Enable {label}", exact=True).uncheck()
            editor.get_by_label("Title weight", exact=True).fill("100")
            with page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == PREVIEW_URL) as response:
                editor.get_by_role("button", name="Preview ranking", exact=True).click()
            assert response.value.status == 200
            body = response.value.json()
            assert body["current_version"] == saved["version"]
            results = page.get_by_label("Draft ranking preview results", exact=True)
            expect(results).to_contain_text("Preview only ? not saved")
            table = page.get_by_role("table", name="Draft weight ranking comparison", exact=True)
            aligned = table.get_by_role("row").filter(has_text="Systems Administrator")
            expect(aligned).to_contain_text("+25")
            expect(aligned).to_contain_text("100")
            assert snapshot(store) == before

            editor.get_by_label("Title weight", exact=True).fill("50")
            expect(results).to_have_count(0)
            hold_next = True
            with page.expect_request(lambda r: r.method == "POST" and urlsplit(r.url).path == PREVIEW_URL):
                editor.get_by_role("button", name="Preview ranking", exact=True).click()
            expect(editor.get_by_role("button", name="Calculating preview?", exact=True)).to_be_disabled()
            assert len(held) == 1
            editor.get_by_label("Title weight", exact=True).fill("25")
            held[0].fulfill(status=200, content_type="application/json", body=json.dumps(body))
            expect(editor.get_by_role("button", name="Preview ranking", exact=True)).to_be_enabled()
            expect(results).to_have_count(0)
            page.get_by_label("Saved search profile", exact=True).last.select_option("")
            expect(results).to_have_count(0)
            assert len(previews) == 2
            assert outside == [] and forbidden == []
            assert snapshot(store) == before
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        store.engine.dispose()
        assert not thread.is_alive(), "Owned preview fixture server did not stop"
