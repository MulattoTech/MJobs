"""End-to-end Mission Control acceptance history, expand/collapse and timeline hover."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import threading
import time
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import uvicorn

from tools.agent_dashboard.acceptance_history import AcceptanceHistory
from tools.agent_dashboard.model_catalog import ModelCatalog, _choice
from tools.agent_dashboard import server as mission_server


def test_acceptance_history_browser(tmp_path):
    repo=Path(__file__).resolve().parents[2]
    db=tmp_path/"acceptance-browser.db"
    with patch.object(ModelCatalog, "_discover", return_value=(
        [_choice("gpt-6-luna","Luna",["high"],"high")],"test-fixture",
    )), patch.object(mission_server, "AcceptanceHistory",
                   side_effect=lambda root:AcceptanceHistory(root,db)):
        app=mission_server.create_app(repo,mission_instance=True)
    current=app.state.mission_control.progress()
    assert current["available"]
    areas=[{"name":x["name"],"pass":x["pass"],"total":x["total"]}
           for x in current["areas"]]
    priorities=[{"name":x["name"],"pass":x["pass"],"total":x["total"]}
                for x in current["priorities"]]
    first_area=next(a for a in areas if a["pass"]>0)
    first_priority=next(p for p in priorities if p["pass"]>0)
    first_area["pass"]-=1
    first_priority["pass"]-=1
    old_time=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    baseline={
        "provenance":"user_screenshot","observed_at":old_time,
        "provenance_note":"Synthetic UI test fixture, not a real historical checkpoint",
        "original":{"pass":current["original"]["pass"]-1,"total":current["original"]["total"]},
        "reference":{"pass":current["reference"]["pass"],"total":current["reference"]["total"]},
        "areas":areas,"priorities":priorities,
    }
    app.state.acceptance_history.import_screenshot(baseline)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    web=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,
                                    log_level="error",access_log=False))
    worker=threading.Thread(target=web.run,daemon=True)
    worker.start()
    deadline=time.monotonic()+15
    while not web.started and time.monotonic()<deadline:
        time.sleep(.05)
    assert web.started
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                page=browser.new_page(viewport={"width":1510,"height":990})
                page.set_default_timeout(13000)
                errors=[]
                agent_launches=[]
                page.on("pageerror",lambda e:errors.append(str(e)))
                page.on("request",lambda req:agent_launches.append(req.url)
                        if req.method=="POST" and req.url.endswith("/launch") else None)
                page.goto(f"http://127.0.0.1:{port}/",wait_until="domcontentloaded")
                page.locator("#acceptanceBaseline option").nth(1).wait_for(state="attached")
                assert page.locator("#acceptanceSparkline circle").count()==2
                assert page.locator("#acceptanceReferenceList details").count()==current["reference"]["total"]
                assert page.request.post(f"http://127.0.0.1:{port}/api/progress/checkpoint").status==403
                page.get_by_role("button",name="Save checkpoint now").click()
                page.locator("#acceptanceBaseline option").nth(2).wait_for(state="attached",timeout=15000)
                assert page.locator("#acceptanceSparkline circle").count()==3
                assert page.locator("#acceptanceTimelineLabel").inner_text()
                assert "+1 accepted" in page.locator("[data-history-summary=original]").inner_text()
                meter=page.locator("#releaseContent [data-history-kind=original] .release-meter")
                meter.hover()
                page.locator("#acceptanceHoverTooltip:not([hidden])").wait_for()
                assert "Earlier" in page.locator("#acceptanceHoverTooltip").inner_text()
                page.get_by_role("button",name="Collapse all").click()
                assert page.locator("#releaseTracker details[open]").count()==0
                page.get_by_role("button",name="Expand all").click()
                assert page.locator("#releaseTracker details[open]").count()>20
                row=page.locator(".release-group[data-disclosure-key='group:areas'] .area-progress").first
                row.locator("summary").click()
                open_before=row.get_attribute("open") is not None
                page.wait_for_timeout(11500)
                assert (row.get_attribute("open") is not None)==open_before
                page.locator("#acceptanceMetric").select_option("priority:P0")
                assert "P0" in page.locator("#acceptanceTimelineLabel").inner_text()
                page.locator("#acceptanceRange").fill("1")
                page.locator("#acceptanceRange").dispatch_event("input")
                assert page.locator("#acceptanceBaseline").input_value()!="1"
                assert not agent_launches
                assert not errors, errors
            finally:
                browser.close()
    finally:
        web.should_exit=True
        worker.join(timeout=8)
