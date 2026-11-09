"""Connected local Chromium journey: edit native weights, reload and explain ranking."""
from datetime import datetime, timedelta
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from tests.integration.test_preference_weighting import (
    CONFIG_URL, app_for, create_workspace, criteria, rescore,
)


def test_preferences_save_reload_and_show_real_persisted_contributions(tmp_path):
    store = create_workspace(tmp_path)
    app = app_for(store, tmp_path)
    sock = bound_browser_socket()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    outside_requests, forbidden_writes, saved_requests = [], [], []
    pending_view_paths, recorded_view_paths = [], []
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Isolated API did not become ready"
            time.sleep(0.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()

            def boundary(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != urlsplit(origin).netloc:
                    outside_requests.append(request.url)
                    route.abort()
                elif request.method == "PUT" and parsed.path == CONFIG_URL:
                    saved_requests.append(request.post_data_json)
                    route.continue_()
                elif (request.method == "POST" and pending_view_paths
                      and request.url == f"{origin}{pending_view_paths[0]}"
                      and request.post_data is None):
                    recorded_view_paths.append(pending_view_paths.pop(0))
                    route.continue_()
                elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                    forbidden_writes.append((request.method, parsed.path))
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
            expect(editor.get_by_label("Role family", exact=True)).to_be_enabled()
            editor.get_by_label("Role family", exact=True).fill("Synthetic preference weighting")
            labels = ("Skills", "Title", "Salary", "Work arrangement", "Employment type", "Remote work region")
            for label in labels:
                editor.get_by_label(f"Enable {label}", exact=True).check()
                editor.get_by_label(f"{label} weight", exact=True).fill("50" if label == "Skills" else "10")
            with page.expect_response(lambda r: r.request.method == "PUT" and urlsplit(r.url).path == CONFIG_URL) as saved:
                editor.get_by_role("button", name="Save profile", exact=True).click()
            assert saved.value.status == 200
            version = saved.value.json()["profile"]["version"]
            expect(editor).to_contain_text(f"Saved and verified Synthetic preference weighting profile version {version}")
            assert criteria(store)["role_scoring_profile"]["weights"]["remote_location"] == 10
            rescore(store)

            page.reload(wait_until="domcontentloaded")
            with page.expect_response(lambda r: urlsplit(r.url).path == "/jobs") as listed:
                page.get_by_label("Saved search profile", exact=True).last.select_option("preference-search")
            assert [row["id"] for row in listed.value.json()["jobs"]] == ["aligned-job", "unknown-job", "mismatch-job"]
            editor = page.locator("section").filter(has=page.get_by_role(
                "heading", name="Role-specific scoring profile", exact=True))
            expect(editor.get_by_label("Remote work region weight", exact=True)).to_have_value("10")
            expect(editor.get_by_label("Title weight", exact=True)).to_have_value("10")
            view_path = "/profiles/fixture/jobs/aligned-job/view"
            with (
                page.expect_response(lambda r:
                    r.request.method == "POST" and r.url == f"{origin}{view_path}"
                ) as viewed,
                page.expect_response(lambda r:
                    urlsplit(r.url).path.endswith("/aligned-job/match-explanation")
                ) as explained,
            ):
                # Permit one bodyless view request only for this explicit detail open.
                pending_view_paths.append(view_path)
                page.locator(".job-card").filter(has_text="Systems Administrator").click()
            view_receipt = viewed.value.json()
            assert viewed.value.status == 201
            assert set(view_receipt) == {"profile_id", "job_id", "viewed_at"}
            assert view_receipt["profile_id"] == "fixture"
            assert view_receipt["job_id"] == "aligned-job"
            assert datetime.fromisoformat(view_receipt["viewed_at"]).utcoffset() == timedelta(0)
            body = explained.value.json()
            assert explained.value.status == 200
            assert body["score"] == 75
            assert body["scoring_profile"]["version"] == version
            expect(page.get_by_label("Saved match score", exact=True)).to_contain_text("75 / 100")
            contributions = page.get_by_role("table", name="Scoring signal contributions", exact=True)
            expect(contributions).to_contain_text("DETERMINISTIC_PROFILE_JOB_FIT")
            expect(contributions).to_contain_text("match:fit_factors/remote_location")
            # Inspect every scope so a second job/profile write cannot be hidden.
            with store.engine.connect() as connection:
                history = [dict(row) for row in connection.execute(select(
                    m.profile_job_views.c.profile_id, m.profile_job_views.c.job_id,
                    m.profile_job_views.c.viewed_at, m.profile_job_views.c.visit_order,
                )).mappings()]
            assert history == [{**view_receipt, "visit_order": 1}]
            assert pending_view_paths == []
            assert recorded_view_paths == [view_path]
            assert len(saved_requests) == 1
            assert outside_requests == [] and forbidden_writes == []
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        store.engine.dispose()
        assert not thread.is_alive(), "Owned fixture server did not stop cleanly"
