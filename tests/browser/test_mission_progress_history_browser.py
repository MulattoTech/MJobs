"""Real Chromium smoke for Mission Control's expandable time-travel acceptance UI."""
from __future__ import annotations
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import uvicorn

from tools.agent_dashboard.model_catalog import ModelCatalog,_choice
from tools.agent_dashboard.server import create_app

ROOT=Path(__file__).resolve().parents[2]

def test_historical_dashboard_expand_hover_and_capture(tmp_path):
    with patch.object(ModelCatalog,"_discover",return_value=(
        [_choice("gpt-6-luna","Luna",["high"],"high")],"fixture"
    )):
        app=create_app(ROOT,mission_instance=True,progress_history_db=tmp_path/"acceptance.db")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            port=sock.getsockname()[1]
        config=uvicorn.Config(app,host="127.0.0.1",port=port,log_level="error")
        server=uvicorn.Server(config)
        thread=threading.Thread(target=server.run,daemon=True)
        thread.start()
        try:
            deadline=time.monotonic()+16
            while not server.started and time.monotonic()<deadline:
                time.sleep(.1)
            assert server.started,"Test server did not start"
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                try:
                    page=browser.new_page(viewport={"width":1600,"height":1000})
                    page.set_default_timeout(17000)
                    errors=[]
                    page.on("pageerror",lambda err:errors.append(str(err)))
                    response=page.goto(f"http://127.0.0.1:{port}/",wait_until="domcontentloaded")
                    assert response.status==200
                    page.locator("#progressTimeTravel").wait_for(state="visible")
                    page.locator("#progressHistorySelect option").first.wait_for(state="attached")
                    assert page.locator(".progress-card-details").count()==3
                    assert page.locator(".progress-group").count()==2
                    assert page.locator(".progress-group-item").count()>=20
                    page.locator("#progressCaptureNow").click()
                    page.wait_for_function("() => document.querySelectorAll('#progressHistorySelect option').length >= 2",timeout=15000)
                    options=page.locator("#progressHistorySelect option").count()
                    assert options>=2
                    page.locator("#progressHistorySelect").select_option(index=0)
                    page.locator(".progress-group-item").first.locator("summary").click()
                    details=page.locator(".progress-group-item").first
                    assert details.get_attribute("open") is not None
                    assert details.locator(".progress-item-list .progress-item").count()>0
                    hover=details.locator(".area-progress .bar")
                    assert "selected" in (hover.get_attribute("title") or "")
                    page.locator("#progressCollapseAll").click()
                    page.wait_for_timeout(250)
                    assert page.locator("#releaseContent details.progress-expandable[open]").count()==0
                    page.locator("#progressExpandAll").click()
                    page.wait_for_timeout(250)
                    assert page.locator("#releaseContent details.progress-expandable[open]").count()>10
                    assert page.locator(".progress-card-details").nth(1).inner_text().find("MJ-REF")!=-1
                    assert not errors,errors
                finally:
                    browser.close()
        finally:
            server.should_exit=True
            thread.join(timeout=10)
