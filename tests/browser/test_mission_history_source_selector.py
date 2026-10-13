"""Mission Control source switching is clearly labeled and read-only."""
from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import json
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "tools" / "agent_dashboard" / "static"


class StaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, url: str) -> str:
        path = urlsplit(url).path
        if path in {"/", "/index.html"}:
            return str(STATIC / "index.html")
        if path.startswith("/static/"):
            requested = (STATIC / path[len("/static/"):]).resolve()
            if requested.is_relative_to(STATIC.resolve()):
                return str(requested)
        return str(STATIC / "missing")


def fixture(source: str):
    is_gh = source == "github"
    sha = ("a" * 40 if is_gh else "b" * 40 if source == "all" else "c" * 40)
    msg = {
        "all": "feat: other-branch progress at 8:47 PM",
        "checkout": "test: current checkout checkpoint at 7:54 PM",
        "github": "Merge pull request #611 at 8:19 PM",
    }[source]
    return {
        "available": True, "source": {
            "all":"LOCAL_ALL_KNOWN_REFS",
            "checkout":"LOCAL_GIT_COMMIT_HISTORY",
            "github":"GITHUB_MAIN_PUBLISHED",
        }[source],
        "history_source":source,"head":sha,"branch":"main" if is_gh else source,
        "observed_at":"2026-10-13T01:40:00Z",
        "refresh_seconds":45 if is_gh else 5,
        "source_description": {
            "all":"All local and cached remote refs; unpushed work may appear.",
            "checkout":"Current checkout only.",
            "github":"Published commits on GitHub main; feature branches appear after merge."
        }[source],
        "commits":[{
            "sha":sha,"short_sha":sha[:10],"timestamp":"2026-10-13T01:19:00Z",
            "author":"Test user","subject":msg,
            "category":"OTHER" if is_gh else "PRODUCT_FEATURE",
            "category_explanation":"Metadata only" if is_gh else "Product feature",
            "area":"GitHub main" if is_gh else "Web interface",
            "files_changed":None if is_gh else 1,
            "added":None if is_gh else 10,"deleted":None if is_gh else 0,
            "files":[] if is_gh else [{"path":"apps/web/src/main.tsx",
                                        "additions":10,"deletions":0}],
            "files_truncated":0,"metadata_only":is_gh,"issue_refs":["#611"] if is_gh else []
        }],
        "sample_size":1,
        "categories":{"OTHER":1} if is_gh else {"PRODUCT_FEATURE":1},
        "classification_available":not is_gh,
        "code_commits":None if is_gh else 1,
        "change_total":None if is_gh else 10,
        "codex":{"available":False}
    }


def test_source_selector_distinguishes_all_local_refs_and_github():
    server=ThreadingHTTPServer(("127.0.0.1",0),StaticHandler)
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                page=browser.new_page(viewport={"width":1500,"height":1000})
                launches=[]
                history_sources=[]
                page.on("request",lambda req:launches.append(req.url)
                        if "/api/launcher/" in req.url and req.method=="POST"
                        else None)
                def intercept(route):
                    from urllib.parse import urlsplit,parse_qs
                    source=parse_qs(urlsplit(route.request.url).query).get("source",["checkout"])[0]
                    history_sources.append(source)
                    route.fulfill(status=200,content_type="application/json",
                                  body=json.dumps(fixture(source)))
                page.route("**/api/history?*",intercept)
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.locator("#historySource").select_option("all")
                page.get_by_text("other-branch progress at 8:47 PM",exact=False).wait_for(timeout=12000)
                assert page.locator("#historyProduct").inner_text()=="1"
                page.locator("#historySource").select_option("github")
                page.get_by_text("Merge pull request #611 at 8:19 PM",exact=False).wait_for(timeout=12000)
                assert page.locator("#historyProduct").inner_text()=="N/A"
                assert page.locator("#historyBookkeeping").inner_text()=="N/A"
                assert page.get_by_text("Changed files not inspected",exact=False).count()>0
                assert page.locator("#historyCategory").is_disabled()
                assert "Published commits on GitHub main" in page.locator("#historySourceNote").inner_text()
                page.locator("#historySource").select_option("checkout")
                page.get_by_text("current checkout checkpoint at 7:54 PM",exact=False).wait_for(timeout=12000)
                assert page.locator("#historyCategory").is_enabled()
                assert {"all","github","checkout"}.issubset(set(history_sources))
                assert not launches
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
