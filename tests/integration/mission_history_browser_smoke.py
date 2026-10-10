"""Live browser history panel acceptance without starting AI or mutating Git."""
from __future__ import annotations
from pathlib import Path
from playwright.sync_api import sync_playwright
import argparse
import json

parser=argparse.ArgumentParser(description="Read-only Mission Control history browser acceptance")
parser.add_argument("--url", default="http://127.0.0.1:18726/")
arguments=parser.parse_args()
URL=arguments.url.rstrip("/")+"/"
OUT = Path.home() / "AppData" / "Local" / "Temp" / "mjobs_history_browser_20261010.png"

with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True)
    context=browser.new_context(viewport={"width":1650,"height":1080},device_scale_factor=1)
    page=context.new_page()
    errors=[];launches=[];history_requests=[]
    page.on("pageerror",lambda e:errors.append(str(e)))
    page.on("request",lambda req:
        launches.append(req.url) if req.url.endswith("/launch") else
        (history_requests.append(req.url) if "/api/history" in req.url else None))
    response=page.goto(URL,wait_until="domcontentloaded",timeout=23000)
    page.locator(".history-entry").first.wait_for(timeout=45000)
    print("HTTP",response.status)
    print("HISTORY_VISIBLE",page.locator("#historyTracker").is_visible())
    print("INITIAL_ROWS",page.locator(".history-entry").count())
    print("SAMPLE",page.locator("#historySample").inner_text())
    print("PRODUCT_COUNT",page.locator("#historyProduct").inner_text())
    print("DOCS_COUNT",page.locator("#historyBookkeeping").inner_text())
    print("CATEGORY_METERS",page.locator("#historyMeter span").count())
    first=page.locator(".history-entry").first
    print("FIRST_COMMIT",first.locator(".history-entry-subject").inner_text()[:100])
    first.locator("details").locator("summary").click()
    print("FILE_DETAILS_OPEN",first.locator("details").get_attribute("open") is not None)
    print("FILE_DETAIL_CHARS",len(first.locator(".history-files pre").inner_text()))
    page.locator("#historyCategory").select_option("BOOKKEEPING")
    page.wait_for_timeout(150)
    docs=page.locator(".history-entry").count()
    print("BOOKKEEPING_ROWS",docs)
    assert docs>0
    # Filtering changes the visible result set without mutating the backend.
    page.locator("#historyCategory").select_option("CORE_FEATURE")
    page.wait_for_timeout(150)
    core=page.locator(".history-entry").count()
    print("CORE_FEATURE_ROWS",core)
    assert core>0
    page.locator("#historyCategory").select_option("ALL")
    page.locator("#historyLimit").select_option("25")
    page.wait_for_function("""() => document.querySelectorAll('.history-entry').length<=25""",timeout=22000)
    print("LIMIT_25",page.locator(".history-entry").count())
    assert page.locator(".history-entry").count()<=25
    page.locator("#historyLimit").select_option("50")
    page.wait_for_function("""() => document.querySelectorAll('.history-entry').length>=26""",timeout=22000)

    # Inject test-only changed Git history response to check automatic polling UI.
    original=context.request.get(URL+"api/history?limit=50")
    assert original.ok
    base=original.json()
    changed=json.loads(json.dumps(base))
    template=changed["commits"][0]
    fake={**template,"sha":"f"*40,"short_sha":"f"*10,
          "subject":"feat: browser-only new history entry check",
          "category":"CORE_FEATURE","category_explanation":"Multi-component product feature",
          "area":"Test-only","files_changed":1,
          "files":[{"path":"apps/api/browser_fixture.py","additions":2,"deletions":0,"kind":"PRODUCT"}],
          "files_truncated":0,"added":2,"deleted":0,"production_lines_changed":2}
    changed["commits"]=[fake]+changed["commits"][:-1]
    changed["head"]=fake["sha"]
    changed["categories"]["CORE_FEATURE"]=changed["categories"].get("CORE_FEATURE",0)+1
    changed["observed_at"]="2026-10-10T16:45:00Z"
    # Route only this Playwright browser's GET; no data is changed on disk/server.
    intercepted=[0]
    def history_route(route):
        intercepted[0]+=1
        route.fulfill(status=200,content_type="application/json",body=json.dumps(changed))
    page.route("**/api/history?*",history_route)
    page.wait_for_function("""() => document.querySelector('.history-entry .history-entry-subject')?.textContent?.includes('browser-only new history')""",timeout=12000)
    print("AUTOPOLL_DETECTED_UPDATED_HISTORY",True)
    print("NEW_NOTICE",page.locator("#historyNewCommitNotice").inner_text())
    print("HISTORY_REQUESTS",len(history_requests),"INJECTED_RESPONSE_COUNT",intercepted[0])
    page.screenshot(path=str(OUT),full_page=True)
    print("SCREENSHOT",OUT,"BYTES",OUT.stat().st_size)
    print("PAGE_ERRORS",errors)
    print("AGENT_LAUNCH_REQUESTS",launches)
    assert not errors
    assert not launches
    browser.close()
